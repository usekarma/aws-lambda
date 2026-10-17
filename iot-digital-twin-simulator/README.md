# iot-digital-twin-simulator runtime

Consumes the existing simulator/traces rather than copying their rules. DynamoStore
uses strongly consistent reads, revision/epoch/fence conditions, atomic reset + receipt
transactions, persisted ACK budgets and five-second publication claims. Receipts precede
publication; a publish/mark-sent crash may duplicate an ACK but never repeats the reset.

Handlers: app.start_handler (IAM-only initializer), app.runner_handler (bounded execution
and lease renewal), app.reset_handler (trusted IoT projection). No HTTP adapter added.
Required environment: STATE_TABLE, MODEL_COMMIT, ACTIVE_SESSION (fresh 32-hex ID),
EXPECTED_PRINCIPAL, REQUEST_TOPIC, ACK_TOPIC. MODEL_COMMIT must match package provenance
and persisted state. Five assets, seed 17, six ticks/epoch, 180-second session, three
resets maximum. Dynamo SDK attempts total two; IoT publication one per durable claim.

Run make test-demo MODEL_REPO=/path/to/iot-digital-twin. Package with:
python3 scripts/package_demo.py --model-repo /path/to/iot-digital-twin --model-commit FULL_REVIEWED_SHA
ZIP includes runtime/model imports and provenance. No extra model implementation.
AWS provides boto3/botocore; clients are lazy and injected in local tests.

Controlled integration: scripts/test_demo_integration.py refuses AWS calls unless
--execute-approved-writes, profile, region, expected account, table and model revision
are provided. Verify separate human approval for exact writes first. It checks STS,
sandbox tag and PK/SK schema, writes a new isolated session in an EXISTING table,
checks real conditional updates/reset receipts and records ACK LOCALLY (no MQTT).
Session rows are retained for approved cleanup; no resources/permissions are changed.
PYTHONPATH must point at the same reviewed simulator or extracted runtime package.

Limits: no live AWS proof. Mock tests inspect API requests, not actual transaction
execution. Runner does not dispatch pending ACKs: reset Lambda retries or supervised
same-command redelivery recover failed publication within deadline/three-claim bounds.
No HTTP dashboard, automatic restart, device subscription or LED proof implemented.
Trust requires scoped IoT invocation and explicit principal()/topic() projection; direct
Lambda invokers must not be able to forge reset authority. SDK implementation is in
aws-lambda; Terraform/IAM in aws-iac; settings/artifacts in aws-config.

Runner invocation: {session_id: ACTIVE_SESSION, operation: start} initializes once
then runs; {session_id: ACTIVE_SESSION} recovers an expired lease within its unchanged
deadline. These IAM-only commands are not HTTP endpoints. Artifact pin:
8e2f732127ce19936711346d3c1a50ed3b9b728a. Nine mocked-client tests passed both from
source and the built ZIP; 17 existing ingestion tests passed. Ruff 0.11.13 checks/format
and Bandit runtime scan passed. Two builds produced identical artifact SHA-256:
70c95cde1b448c6daf405ff680da06c971b30ad625270524d7752e9df7168cab.
Integration guard without execution flag exited 2 before SDK import or AWS calls.
No real DynamoDB/IoT test was executed. The companion aws-iac component provides
review-only concrete table/Lambda/IAM/rule wiring; aws-config declaration and resolved
artifact/principal/account/session bindings remain prerequisites for an approved plan.

Naming revision: deployment component and ZIP basename are iot-digital-twin-simulator.
Project is iot-digital-twin; Experiment is atom-lite-simulator. Runtime calculations,
state/reset/ACK contracts and packaged ZIP bytes are unchanged. Rechecked all 26
source tests, nine ZIP tests, Ruff and Bandit after renaming; no AWS calls were made.
