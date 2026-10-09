# AWS Lambda runtimes

`aws-lambda` owns runtime implementation and packaging. This repository contains
application code, tests, and local artifact builds; it does not provision or deploy
AWS resources.

- `aws-iac` owns the Lambda resource, IAM, IoT trigger, DynamoDB and deployment wiring.
- `aws-config` owns desired state, including the configuration consumed by that wiring.
- Certificates and TwinMaker integration are out of scope for this PR.

## IoT digital-twin ingestion

The first runtime implements M5Stack Core2 → MQTT/TLS → AWS IoT Core → IoT Rule →
Lambda → DynamoDB latest state. Source lives in `iot-digital-twin-ingest/`:

- `app.py`: lazy AWS initialization, injectable clock/repository, safe structured logs.
- `validation.py`: schema, identity, time, numeric and operating-state validation.
- `domain.py`: environment configuration and deterministic condition derivation.
- `repository.py`: DynamoDB serialization and atomic conditional writes.
- `tests/test_ingest.py`: credential-free unit tests and adapter contract tests.

### Local commands and artifact

Requires Python 3.12 and Make. Install the pinned development tool once:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test
make lint
make package
```

Tests use the standard library and do not need boto3, AWS credentials or network.
Lint uses Ruff 0.11.13 (checks and formatting). Packaging uses the standard library,
fixed ZIP timestamps, stable file order and permissions, and excludes tests and
local environment files. Output: `dist/iot-digital-twin-ingest.zip`.
It contains `app.py`, `domain.py`, `validation.py`, `repository.py` at the ZIP root.
Handler: `app.lambda_handler`; runtime: AWS Python 3.12. The artifact uses the
AWS-runtime-provided boto3; application requirements are empty. For reproducible
bytes, use the same Python/zlib build. The SDK version is managed by AWS and is
not pinned in this artifact.

### Environment configuration

| Variable | Required/default | Meaning |
| --- | --- | --- |
| `EXPECTED_DEVICE_ID` | Required | Expected thing/device name; one device per configured runtime |
| `EXPECTED_PRINCIPAL` | Required | Exact trusted identity value returned by the configured IoT rule's `principal()` function |
| `LATEST_STATE_TABLE` | Required | Existing DynamoDB table name |
| `MAX_CLOCK_SKEW_SECONDS` | `10` | Maximum absolute difference between telemetry time and server receive time |
| `ACCEL_MOTION_THRESHOLD_G` | `0.15` | Motion if acceleration magnitude differs from 1 g by more than this value |
| `GYRO_MOTION_THRESHOLD_DPS` | `5` | Motion if gyro magnitude exceeds this value |

Numeric configuration must be finite and nonnegative. Missing/invalid configuration
fails closed. No account, region, ARN, credential or certificate is embedded.
The AWS SDK uses the Lambda execution role and infrastructure-provided region.

### Input and trust contract

The Lambda receives one decoded JSON object, for example:

```json
{
  "device_id": "core2-aws-001",
  "timestamp": "2026-10-08T19:45:00Z",
  "accel_x": 0.03,
  "accel_y": -0.02,
  "accel_z": 1.01,
  "gyro_x": 0.4,
  "gyro_y": 0.1,
  "gyro_z": -0.2,
  "operating_state": "NORMAL",
  "sequence": 42,
  "principal": "<rule-injected identity>",
  "topic": "devices/core2-aws-001/telemetry"
}
```

All shown fields are required. The payload `device_id` is data, never authority.
The runtime requires it to match `EXPECTED_DEVICE_ID`, requires `principal` to match
`EXPECTED_PRINCIPAL`, and requires the exact topic
`devices/<EXPECTED_DEVICE_ID>/telemetry`. Device IDs use 1–128 ASCII letters,
digits, underscores or hyphens, starting with a letter or digit.

**Trust is established by infrastructure, not the event object's shape.** The IoT
rule must explicitly project telemetry fields and independently inject `principal()`
and `topic()`. Do not use `SELECT *` or accept device-supplied identity aliases.
For example, infrastructure can consume this projection (not provisioning code):

```sql
SELECT device_id, timestamp, accel_x, accel_y, accel_z,
       gyro_x, gyro_y, gyro_z, operating_state, sequence,
       principal() AS principal, topic() AS topic
FROM 'devices/+/telemetry'
```

`aws-iac` must verify the provisioned principal-to-thing association and configure
the corresponding exact principal value. `principal()` identifies the connection
principal; it is not a thing-name claim. Scope MQTT publish permissions to the
thing's telemetry topic and restrict Lambda invocation to the intended IoT rule
with source-account/source-ARN conditions. The runtime cannot distinguish a forged
object submitted by an independently authorized Lambda invoker. These protections
are therefore mandatory before production use. See the
[AWS IoT SQL functions reference](https://docs.aws.amazon.com/iot/latest/developerguide/iot-sql-functions.html).

Timestamps must use RFC3339 UTC (`Z` or `+00:00`), with optional 1–6 fractional
second digits; invalid dates, non-UTC offsets and leap seconds are rejected.
Past and future skew are checked symmetrically against the injected server clock;
the limit is inclusive. IMU units are acceleration in g and gyro in degrees/second.
Each acceleration axis must be in [-16, 16]; each gyro axis in [-2000, 2000],
matching common Core2 sensor full-scale ranges. These are plausibility limits,
not calibration guarantees. JSON numeric values only: booleans, numeric strings,
nulls, NaN, Infinity and out-of-range values are rejected.

`operating_state` currently permits only `NORMAL`. Device-authored `OFFLINE` is
explicitly rejected. Sequence is an integer in [0, 2^63−1], excluding booleans.
Unknown fields are discarded, including authored `condition` and `connectivity`.

### Latest-state and response contracts

Only fully validated telemetry is written. The persisted item contains:

- `device_id` (string partition key), `sequence` (last accepted integer), UTC `timestamp`.
- All six normalized numeric IMU fields and `operating_state`.
- `condition`: `MOVING` if either configured magnitude threshold is exceeded,
  otherwise `STATIONARY`; equality is stationary.
- `received_at`: server UTC receive time.
- `connectivity`: `{ "status": "ONLINE", "source": "accepted_telemetry" }`,
  derived by the server from the accepted message.

Principal and topic are neither persisted nor logged. Floating-point values are
converted to Decimal for DynamoDB. This runtime does not accept additional trusted
connectivity metadata yet; it derives its own ONLINE observation. An infrastructure
liveness process must derive OFFLINE from absence of telemetry. Connectivity owned
by another writer should be stored separately: this runtime replaces the latest
telemetry item in full on acceptance.

A single conditional `PutItem` uses:

```text
attribute_not_exists(#device) OR #sequence < :sequence
```

The first sequence is accepted; thereafter only strictly higher sequences can
replace the item. DynamoDB evaluates the condition atomically against persisted
state, so there is no read-before-write race. Duplicate/stale sequence conditions
return a rejection; they cannot overwrite newer telemetry. See
[DynamoDB PutItem](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_PutItem.html).
An existing item without a sequence fails closed. Do not TTL/delete this item while
its replay watermark is needed. Devices must retain sequence across reboot;
sequence reset/epoch migration requires a separate trusted operational design.

Success returns `{ "status": "accepted", "device_id": "...", "sequence": 42,
"condition": "STATIONARY" }`. Validation/replay rejection returns
`{ "status": "rejected", "reason": "<fixed reason code>" }`. Reasons include
`missing_field`, `invalid_event`, `invalid_device_id`, `identity_mismatch`,
`invalid_timestamp`, `clock_skew`, `invalid_imu`, `invalid_operating_state`,
`device_offline_forbidden`, `invalid_sequence`, and `stale_sequence`.
The handler logs only these safe results. Unexpected configuration/SDK/storage
failures log a fixed error and raise a sanitized exception so invocation retry
remains possible; validation rejections return normally. With asynchronous IoT
invocations the response is diagnostic, not an MQTT acknowledgement. AWS IoT/Lambda
service-level logs must independently be configured to avoid sensitive event data.

### Infrastructure handoff and limits

`aws-iac` needs to consume the ZIP, handler and Python runtime above; supply the
required environment variables; create the existing-table contract (string
`device_id` partition key, no sort key); grant only necessary `dynamodb:PutItem`
access and logging permissions; and wire the trusted IoT projection and scoped
invocation/publish policies. It owns deployment, retries, failure destinations,
monitoring, encryption, retention and server-side liveness detection. `aws-config`
provides desired identity bindings and threshold/table configuration.

This bootstrap supports one configured device identity, a simple uncalibrated
motion heuristic and latest state only. It does not perform registry lookups,
certificate management, history storage, multi-device identity mapping, offline
scheduling or TwinMaker integration. Tests verify domain behavior and SDK request
shape using fakes; no live AWS integration has been run. No AWS resources are
created or deployed by any command in this repository.
