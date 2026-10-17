import copy
import json
import unittest
from unittest.mock import Mock

from runtime import Conflict, DynamoStore, projection

SESSION = "a" * 32


class ConditionalError(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class TransactionError(Exception):
    response = {"Error": {"Code": "TransactionCanceledException"}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = DynamoStore(self.client, "fixture-table", "f" * 40)
        self.state = self.store.start(SESSION, 100)
        self.store.read = Mock(return_value=self.state)

    def test_persistence_and_consistent_read(self):
        item = self.client.put_item.call_args.kwargs["Item"]
        self.assertEqual(json.loads(item["document"]["S"])["deadline"], 280)
        store = DynamoStore(self.client, "fixture-table", "f" * 40)
        self.client.get_item.return_value = {"Item": item}
        self.assertEqual(store.read(SESSION), self.state)
        self.assertTrue(self.client.get_item.call_args.kwargs["ConsistentRead"])

    def test_fenced_tick_and_recovery(self):
        state = self.store.acquire(SESSION, "worker", 100)
        result = self.store.tick(state, 100)
        request = self.client.put_item.call_args.kwargs
        self.assertIn("#epoch = :epoch", request["ConditionExpression"])
        self.assertIn("#fence = :fence", request["ConditionExpression"])
        self.assertIn("#lease_until > :now", request["ConditionExpression"])
        self.assertEqual(result["observations"], projection(1, 1)["observations"])
        self.client.put_item.side_effect = ConditionalError()
        with self.assertRaises(Conflict):
            self.store.tick(state, 101)
        with self.assertRaises(Conflict):
            self.store.tick(state, 110)
        self.assertEqual(state["tick"], 0)

    def test_reset_transaction_duplicate_and_no_ack_on_failure(self):
        command = {"session_id": SESSION, "command_id": "reset-1", "expected_epoch": 1}
        self.store.read = Mock(side_effect=[self.state, None])
        receipt = self.store.reset(command, 101, "trusted", "trusted")
        writes = self.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(writes), 2)
        saved = json.loads(writes[0]["Put"]["Item"]["document"]["S"])
        self.assertEqual(
            (saved["epoch"], saved["tick"], saved["deadline"]), (2, 0, 280)
        )
        self.store.read = Mock(side_effect=[saved, receipt])
        self.client.transact_write_items.reset_mock()
        self.assertEqual(self.store.reset(command, 102, "trusted", "trusted"), receipt)
        self.client.transact_write_items.assert_not_called()
        self.store.read = Mock(side_effect=[self.state, None])
        self.client.transact_write_items.side_effect = TransactionError()
        with self.assertRaises(Conflict):
            self.store.reset(command, 101, "trusted", "trusted")

    def test_ack_claim_precedes_publish_and_lost_publish_preserves_receipt(self):
        command = {"session_id": SESSION, "command_id": "reset", "expected_epoch": 1}
        self.store.read = Mock(side_effect=[self.state, None])
        receipt = self.store.reset(command, 101, "trusted", "trusted")
        self.store.read = Mock(side_effect=[self.state, receipt] * 3)
        publisher = Mock()
        order = []
        self.client.update_item.side_effect = lambda **kwargs: order.append("claim")
        publisher.publish.side_effect = lambda **kwargs: order.append("publish")
        ack = self.store.publish_ack(SESSION, "reset", 102, publisher, "fixture/ack")
        self.assertEqual(order, ["claim", "publish", "claim"])
        self.assertEqual(ack["committed_epoch"], 2)
        self.assertFalse(publisher.publish.call_args.kwargs["retain"])
        publisher.publish.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.store.publish_ack(SESSION, "reset", 108, publisher, "fixture/ack")
        self.client.update_item.side_effect = ConditionalError()
        publisher.reset_mock()
        with self.assertRaises(ConditionalError):
            self.store.publish_ack(SESSION, "reset", 114, publisher, "fixture/ack")
        publisher.publish.assert_not_called()
        self.assertEqual(receipt["epoch"], 2)

    def test_stale_sessions_schema_and_limits(self):
        command = {"session_id": SESSION, "command_id": "reset", "expected_epoch": 1}
        with self.assertRaises(ValueError):
            self.store.reset(command, 280, "trusted", "trusted")
        with self.assertRaises(ValueError):
            self.store.reset(command, 100, "forged", "trusted")
        with self.assertRaises(ValueError):
            self.store.reset(
                command | {"expected_epoch": True}, 100, "trusted", "trusted"
            )
        invalid = copy.deepcopy(self.state)
        invalid["model"] = "future"
        with self.assertRaises(ValueError):
            self.store.validate(invalid)
        self.assertEqual(
            projection(2, 1)["trace"]["effects"][0]["predecessor"]["epoch"], 2
        )

    def test_handler_failure_cannot_publish(self):
        import app
        from unittest.mock import patch

        env = {
            "ACTIVE_SESSION": SESSION,
            "REQUEST_TOPIC": "fixture/request",
            "EXPECTED_PRINCIPAL": "trusted",
            "ACK_TOPIC": "fixture/ack",
        }
        store, publisher = Mock(), Mock()
        store.reset.side_effect = Conflict()
        event = {
            "session_id": SESSION,
            "command_id": "reset",
            "expected_epoch": 1,
            "principal": "trusted",
            "topic": "fixture/request",
        }
        with (
            patch.dict("os.environ", env),
            patch.object(app, "clients", return_value=(store, publisher)),
        ):
            with self.assertRaises(Conflict):
                app.reset_handler(event, None)
            store.publish_ack.assert_not_called()
            with self.assertRaises(ValueError):
                app.reset_handler(event | {"session_id": "b" * 32}, None)

    def test_runner_deadline_and_expired_fence_stop(self):
        import app
        from unittest.mock import patch

        store = Mock()
        store.acquire.return_value = self.state | {
            "owner": "worker",
            "fence": 1,
            "lease_until": 110,
        }
        store.read.return_value = self.state | {
            "owner": "other",
            "fence": 2,
            "lease_until": 110,
        }
        context = Mock(aws_request_id="worker")
        context.get_remaining_time_in_millis.return_value = 10000
        with (
            patch.dict("os.environ", {"ACTIVE_SESSION": SESSION}),
            patch.object(app, "clients", return_value=(store, Mock())),
            patch.object(app.time, "monotonic", side_effect=[0, 1]),
            patch.object(app.time, "time", return_value=101),
        ):
            self.assertEqual(
                app.runner_handler({"session_id": SESSION}, context)["status"],
                "stopped",
            )
        store.tick.assert_not_called()
        store.renew.assert_not_called()

    def test_lease_renewal_is_conditional_and_keeps_deadline(self):
        state = self.store.acquire(SESSION, "worker", 100)
        renewed = self.store.renew(state, 106)
        self.assertEqual(renewed["deadline"], 280)
        self.assertEqual(renewed["fence"], state["fence"])
        self.assertIn(
            "#fence = :fence",
            self.client.put_item.call_args.kwargs["ConditionExpression"],
        )
        with self.assertRaises(Conflict):
            self.store.renew(state, 110)

    def test_no_receipt_no_publish_and_model_revision_fails_closed(self):
        self.store.read = Mock(side_effect=[self.state, None])
        publisher = Mock()
        with self.assertRaises(ValueError):
            self.store.publish_ack(SESSION, "absent", 100, publisher, "fixture/ack")
        publisher.publish.assert_not_called()
        self.client.update_item.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.validate(self.state | {"model_commit": "e" * 40})


if __name__ == "__main__":
    unittest.main()
