# First AWS demo: private data layer

This is the first AWS deployment for the project. It creates only:

- One on-demand DynamoDB table keyed by `case_id`.
- One private S3 bucket for case evidence, the illustrative policy, and agent session history.

The app and UI stay on your computer. This template creates no compute, public
endpoint, VPC, load balancer, NAT gateway, CloudWatch log group, or AgentCore
Memory resource. The AWS services are the agent's data sources and session
store; they are not running the Python app.

## What the AWS services do here

- **CloudFormation** reads the template and creates the named AWS resources. A
  change set previews proposed changes before CloudFormation applies them.
- **DynamoDB** stores one structured item per synthetic case. The agent performs
  an exact key lookup; it does not scan the table.
- **S3** stores JSON evidence objects and the policy text. Strands' S3 session
  manager also stores conversation state under `agent-sessions/`.
- **Bedrock** runs the language model only when the app is explicitly started in
  Bedrock mode. It is not part of stack creation or dataset upload.

## Cost boundary

The seed operation writes 1,002 DynamoDB items and 1,003 S3 objects (evidence
for each case plus one policy). Using current US East (N. Virginia) on-demand
rates, the request charges are expected to be pennies or less; data storage is
small. Use the official [DynamoDB pricing](https://aws.amazon.com/dynamodb/pricing/)
and [S3 pricing](https://aws.amazon.com/s3/pricing/) pages to recheck rates before
running it. This estimate excludes Bedrock, tax, unrelated account usage, and
any future compute. AWS Budgets is an alerting tool, not a guaranteed hard cap.

The first stack and dataset upload have completed. An early Bedrock request was
rejected while AWS was still verifying the account. A later bounded request
completed successfully and its token usage was visible in CloudWatch Model
Invocations. This confirms that the configured path can invoke the selected
model; it does not establish a latency distribution, sustainable throughput, or
production capacity. Treat each request as potentially billable and inspect
current model pricing and service quotas before further live calls.

## Create and inspect the stack (PowerShell)

Run commands from the repository root. A random bucket suffix avoids S3's
account-wide bucket-name collisions.

```powershell
$profile = "ai-systems-performance-lab-sso"
$region = "us-east-1"
$stack = "ai-systems-performance-lab-demo"
$table = "ai-systems-performance-lab-cases"
$bucket = "ai-systems-performance-lab-demo-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$template = "infrastructure/aws/demo-data.yaml"

aws cloudformation validate-template --template-body file://$template --profile $profile --region $region
aws cloudformation create-change-set --stack-name $stack --change-set-name initial-data-layer --change-set-type CREATE --template-body file://$template --parameters ParameterKey=DemoBucketName,ParameterValue=$bucket ParameterKey=CasesTableName,ParameterValue=$table --profile $profile --region $region
aws cloudformation wait change-set-create-complete --stack-name $stack --change-set-name initial-data-layer --profile $profile --region $region
aws cloudformation describe-change-set --stack-name $stack --change-set-name initial-data-layer --profile $profile --region $region
```

Review that the preview contains exactly one DynamoDB table and one S3 bucket.
The template does not define IAM resources. Execute only after the preview and
bucket name are what you expect:

```powershell
aws cloudformation execute-change-set --stack-name $stack --change-set-name initial-data-layer --profile $profile --region $region
aws cloudformation wait stack-create-complete --stack-name $stack --profile $profile --region $region
aws cloudformation describe-stacks --stack-name $stack --query "Stacks[0].Outputs" --output table --profile $profile --region $region
```

In the AWS console, select `us-east-1`, open CloudFormation and inspect the
stack's Resources and Events. Then inspect the table in DynamoDB and the bucket's
Block Public Access and encryption settings in S3.

## Generate and upload records

The generator leaves the two hand-written examples intact and adds 1,000
synthetic cases with fixed seed `20261117`. The distribution is stratified
around policy-relevant DSCR ranges to produce varied demonstrations. It is not
intended to represent real applicants or a real customer population.

First run the no-write preview:

```powershell
uv run python scripts/seed_aws_demo.py --profile $profile --region $region --table $table --bucket $bucket
```

Expected plan: 1,002 DynamoDB items and 1,003 S3 objects. The script will not
initialize an AWS session in dry-run mode. Add `--apply` only after checking the
resource names:

```powershell
uv run python scripts/seed_aws_demo.py --profile $profile --region $region --table $table --bucket $bucket --apply
```

Repeating the same seed writes the same item keys and S3 object keys, so partial
uploads can be retried. Verify the item count in DynamoDB and inspect a known
object such as `cases/synthetic-case-000001/evidence.json` in S3.

The local agent case selector uses the same generator defaults and does not
call `Scan`. If you change the generator count or seed, use matching settings
in both the upload command and the app before starting a Bedrock-mode session.

## IAM access needed

Use the existing AWS profile for local development; credentials remain in the
AWS CLI credential store. The upload operation needs `dynamodb:PutItem` on the
demo table and `s3:PutObject` on the demo bucket. The live app needs
`dynamodb:GetItem`, `s3:GetObject` and `s3:PutObject` on the relevant data and
session object paths, plus `s3:ListBucket` scoped to the session prefix for
Strands session history. `s3:DeleteObject` is needed only if deleting individual
sessions through the session manager. Bedrock invocation must be allowed for
the selected inference profile and associated model resources. CloudFormation
deployment needs permissions to create and delete the two resource types.

Do not attach policies to the IAM user automatically. If an operation returns
AccessDenied, inspect that specific permission and request the narrowest
appropriate policy change in the AWS console.

## Run one real agent turn

The previously used inference profile is `us.amazon.nova-2-lite-v1:0` in
`us-east-1`; profile discovery does not prove invoke permission. Check the
current [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) and model
availability before the next call. The current app defaults allow up to six
model calls in a turn, so set the request guard to one for the pilot:

```powershell
$env:AGENT_BACKEND = "bedrock"
$env:AWS_PROFILE = $profile
$env:AWS_REGION = $region
$env:BEDROCK_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
$env:DYNAMODB_CASES_TABLE = $table
$env:AWS_DATA_BUCKET = $bucket
$env:AGENT_SESSION_S3_BUCKET = $bucket
$env:AGENT_MAX_REQUESTS_PER_PROCESS = "1"
$env:AGENT_MAX_MODEL_TURNS = "2"
$env:AGENT_MAX_TOOL_CALLS = "4"
$env:AGENT_MAX_OUTPUT_TOKENS = "400"
$env:BEDROCK_MAX_TOKENS = "512"
uv run uvicorn ai_systems_performance_lab.api.main:app --host 127.0.0.1 --port 8001
```

Start the API and UI from the repository root (`uv run --env-file .env lab`),
choose a generated synthetic case, and ask a bounded question after checking the
model's current price and account status. Configure the profile and usage limits
in `.env`; do not put credentials there.
The activity display shows model success/failure and tool events but not hidden
reasoning text. Stop the API after the answer; the request-count limit resets on
process restart and is not a dollar cap.

## Tear down

Stop the app, then remove objects before asking CloudFormation to delete the
bucket. The bucket name is unique to this stack; verify it before running the
recursive delete.

```powershell
aws s3 ls "s3://$bucket" --profile $profile --region $region
aws s3 rm "s3://$bucket" --recursive --profile $profile --region $region
aws cloudformation delete-stack --stack-name $stack --profile $profile --region $region
aws cloudformation wait stack-delete-complete --stack-name $stack --profile $profile --region $region
```

Confirm that the CloudFormation stack, DynamoDB table, and S3 bucket no longer
exist. Check the Billing console again later because cost and usage reporting can
be delayed. Do not delete unrelated buckets or tables.
