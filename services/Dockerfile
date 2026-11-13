# One image, two targets (built by the CDK in amplify/backend.ts):
#   lambda  Ingest, Insights and API Lambdas; the handler is chosen per function (CMD override).
#   chat    The streaming chat app: uvicorn behind the AWS Lambda Web Adapter (Function URL, RESPONSE_STREAM).
FROM public.ecr.aws/lambda/python:3.13 AS lambda
ENV HOME=/tmp PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-lambda.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements-lambda.txt
COPY shared ${LAMBDA_TASK_ROOT}/shared
COPY ingest ${LAMBDA_TASK_ROOT}/ingest
COPY insights ${LAMBDA_TASK_ROOT}/insights
COPY chat ${LAMBDA_TASK_ROOT}/chat
COPY api ${LAMBDA_TASK_ROOT}/api
CMD ["api.handler.handler"]

FROM lambda AS chat
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
ENV PORT=8080 AWS_LWA_INVOKE_MODE=response_stream AWS_LWA_READINESS_CHECK_PATH=/
ENTRYPOINT ["python", "-m", "uvicorn", "chat.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
CMD []
