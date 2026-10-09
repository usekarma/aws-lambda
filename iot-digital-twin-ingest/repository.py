"""Isolated DynamoDB adapter with atomic monotonic-sequence enforcement."""

from decimal import Decimal


class StaleSequence(Exception):
    pass


def dynamodb_value(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: dynamodb_value(item) for key, item in value.items()}
    return value


class DynamoRepository:
    def __init__(self, table):
        self.table = table

    def put_latest(self, state):
        try:
            self.table.put_item(
                Item=dynamodb_value(state),
                ConditionExpression="attribute_not_exists(#device) OR #sequence < :sequence",
                ExpressionAttributeNames={
                    "#device": "device_id",
                    "#sequence": "sequence",
                },
                ExpressionAttributeValues={":sequence": state["sequence"]},
            )
        except Exception as error:
            response = getattr(error, "response", {})
            if (
                response.get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                raise StaleSequence from None
            raise
