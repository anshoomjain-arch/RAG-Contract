// Amplify Gen 2 backend (SPEC section 2). Deploy with `npx ampx sandbox` (dev) from the repo root.
//
// No Amplify Auth: each browser gets a signed anonymous session token from the API (docs/DECISIONS.md, Phase 6).
// Everything else is plain CDK in one custom stack:
//   S3 (private) --ObjectCreated uploads/*.pdf--> SQS (+DLQ) --> Ingest Lambda --async--> Insights Lambda
//   HTTP API --> API Lambda;  Function URL (RESPONSE_STREAM) --> Chat Lambda (Lambda Web Adapter)
// The Lambdas share one container image (services/Dockerfile). The Neon connection string and the session signing
// key live in one Secrets Manager secret created by scripts/put_secret.py, so they never pass through CloudFormation.
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import { defineBackend } from "@aws-amplify/backend";
import { CfnOutput, Duration, RemovalPolicy, Stack, Tags } from "aws-cdk-lib";
import { CorsHttpMethod, HttpApi, HttpMethod, CfnStage } from "aws-cdk-lib/aws-apigatewayv2";
import { HttpLambdaIntegration } from "aws-cdk-lib/aws-apigatewayv2-integrations";
import { Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { SqsEventSource } from "aws-cdk-lib/aws-lambda-event-sources";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3n from "aws-cdk-lib/aws-s3-notifications";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sqs from "aws-cdk-lib/aws-sqs";

const SERVICES = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "services");
const SECRET_NAME = process.env.RAG_CONTRACT_SECRET ?? "rag-contract/dev";
const LLM_MODEL_ID = process.env.LLM_MODEL_ID ?? "qwen.qwen3-32b-v1:0";
const EMBEDDING_MODEL_ID = process.env.EMBEDDING_MODEL_ID ?? "amazon.titan-embed-text-v2:0";
// Browser origins allowed to call the API, upload and stream chat. "*" until the Amplify Hosting URL exists
// (Phase 7); every call still needs a signed session token.
const ORIGINS = (process.env.ALLOWED_ORIGINS ?? "*").split(",");

const backend = defineBackend({});
const stack = backend.createStack("rag-contract");
const region = Stack.of(stack).region;
Tags.of(stack).add("project", "rag-contract");

// --- storage and queue ---------------------------------------------------------------------------------------
const bucket = new s3.Bucket(stack, "Uploads", {
  blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
  encryption: s3.BucketEncryption.S3_MANAGED,
  enforceSSL: true,
  cors: [{ allowedMethods: [s3.HttpMethods.POST, s3.HttpMethods.GET], allowedOrigins: ORIGINS,
           allowedHeaders: ["*"], maxAge: 3000 }],
  lifecycleRules: [{ abortIncompleteMultipartUploadAfter: Duration.days(1) }],
  removalPolicy: RemovalPolicy.DESTROY,     // sandbox only: deleting the sandbox deletes uploaded test PDFs
  autoDeleteObjects: true,
});

const dlq = new sqs.Queue(stack, "IngestDLQ", { retentionPeriod: Duration.days(14), enforceSSL: true });
const queue = new sqs.Queue(stack, "IngestQueue", {
  visibilityTimeout: Duration.minutes(16),  // longer than the ingest function's 15-minute timeout
  enforceSSL: true,
  deadLetterQueue: { queue: dlq, maxReceiveCount: 2 },
});
bucket.addEventNotification(s3.EventType.OBJECT_CREATED, new s3n.SqsDestination(queue),
                            { prefix: "uploads/", suffix: ".pdf" });

// --- functions -------------------------------------------------------------------------------------------------
const secret = secretsmanager.Secret.fromSecretNameV2(stack, "AppSecret", SECRET_NAME);
const env = {
  DATABASE_SECRET_ID: SECRET_NAME,
  BEDROCK_REGION: region,
  LLM_MODEL_ID,
  EMBEDDING_MODEL_ID,
  EMBEDDING_DIMENSIONS: "1024",
  BUCKET: bucket.bucketName,
};
const bedrock = new iam.PolicyStatement({
  actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
  resources: [LLM_MODEL_ID, EMBEDDING_MODEL_ID].map((m) => `arn:aws:bedrock:${region}::foundation-model/${m}`),
});

function imageFunction(id: string, opts: { target: "lambda" | "chat"; cmd?: string[]; memoryMb: number;
                                           timeout: Duration; extraEnv?: Record<string, string> }) {
  const fn = new lambda.DockerImageFunction(stack, id, {
    code: lambda.DockerImageCode.fromImageAsset(SERVICES, {
      target: opts.target, cmd: opts.cmd, platform: Platform.LINUX_AMD64,
    }),
    architecture: lambda.Architecture.X86_64,
    memorySize: opts.memoryMb,
    timeout: opts.timeout,
    environment: { ...env, ...opts.extraEnv },
    logGroup: new logs.LogGroup(stack, `${id}Logs`, {
      retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: RemovalPolicy.DESTROY,
    }),
  });
  secret.grantRead(fn);
  fn.addToRolePolicy(bedrock);
  return fn;
}

const insights = imageFunction("InsightsFn", {
  target: "lambda", cmd: ["insights.handler.handler"], memoryMb: 1024, timeout: Duration.minutes(15),
});
insights.configureAsyncInvoke({ retryAttempts: 1 });

const ingest = imageFunction("IngestFn", {
  target: "lambda", cmd: ["ingest.handler.handler"], memoryMb: 3008, timeout: Duration.minutes(15),
  extraEnv: { INSIGHTS_FUNCTION: insights.functionName },
});
bucket.grantRead(ingest);
insights.grantInvoke(ingest);
ingest.addEventSource(new SqsEventSource(queue, { batchSize: 1, maxConcurrency: 2 }));

const api = imageFunction("ApiFn", {
  target: "lambda", cmd: ["api.handler.handler"], memoryMb: 512, timeout: Duration.seconds(29),
});
bucket.grantPut(api);
bucket.grantRead(api);
bucket.grantDelete(api);

const chat = imageFunction("ChatFn", { target: "chat", memoryMb: 1024, timeout: Duration.minutes(2) });

// --- endpoints -------------------------------------------------------------------------------------------------
const httpApi = new HttpApi(stack, "Api", {
  corsPreflight: {
    allowOrigins: ORIGINS,
    allowMethods: [CorsHttpMethod.GET, CorsHttpMethod.POST, CorsHttpMethod.DELETE],
    allowHeaders: ["authorization", "content-type"],
    maxAge: Duration.hours(1),
  },
});
httpApi.addRoutes({
  path: "/{proxy+}",
  methods: [HttpMethod.GET, HttpMethod.POST, HttpMethod.DELETE],
  integration: new HttpLambdaIntegration("ApiIntegration", api),
});
// Throttle the whole API (free): a first line of defence before the per-browser limits in the database.
(httpApi.defaultStage!.node.defaultChild as CfnStage).defaultRouteSettings = {
  throttlingRateLimit: 20, throttlingBurstLimit: 40,
};

const chatUrl = chat.addFunctionUrl({
  authType: lambda.FunctionUrlAuthType.NONE,   // the app verifies the signed session token on every request
  invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
  cors: { allowedOrigins: ORIGINS, allowedMethods: [lambda.HttpMethod.POST],
          allowedHeaders: ["authorization", "content-type"], maxAge: Duration.hours(1) },
});

backend.addOutput({
  custom: { apiUrl: httpApi.apiEndpoint, chatUrl: chatUrl.url, region },
});
new CfnOutput(stack, "ApiUrl", { value: httpApi.apiEndpoint });
new CfnOutput(stack, "ChatUrl", { value: chatUrl.url });
new CfnOutput(stack, "IngestDlqName", { value: dlq.queueName });

// --- alarms (Phase 8; SPEC section 11) ---------------------------------------------------------------------------
// Five standard alarms (about $0.10 a month each) with no notification action: their state shows in the CloudWatch
// console. Cost is bounded by the daily limits in the app (services/shared/limits.py), not by a budget alert.
for (const [name, fn] of [["Ingest", ingest], ["Insights", insights], ["Api", api], ["Chat", chat]] as const) {
  new cloudwatch.Alarm(stack, `${name}Errors`, {
    alarmDescription: `rag-contract ${name} Lambda errors (logs carry IDs only; see the function's log group)`,
    metric: fn.metricErrors({ period: Duration.minutes(5), statistic: "Sum" }),
    threshold: 1, evaluationPeriods: 1,
    comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
  });
}
new cloudwatch.Alarm(stack, "IngestDeadLetters", {
  alarmDescription: "rag-contract: an upload failed ingest twice and is in the dead-letter queue",
  metric: dlq.metricApproximateNumberOfMessagesVisible({ period: Duration.minutes(5), statistic: "Maximum" }),
  threshold: 1, evaluationPeriods: 1,
  comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
  treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
});
