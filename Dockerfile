FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY src ${LAMBDA_TASK_ROOT}/src
RUN pip install --no-cache-dir ${LAMBDA_TASK_ROOT}

# Config, prompts and the price table ship in the image, so a deployed image pins all three.
COPY config ${LAMBDA_TASK_ROOT}/config
COPY prompts ${LAMBDA_TASK_ROOT}/prompts
COPY deepseek_pricing.yaml ${LAMBDA_TASK_ROOT}/deepseek_pricing.yaml

# Each function overrides CMD with its own handler (see infra/main.tf).
CMD ["bondhype.lambda_entry.scanner"]
