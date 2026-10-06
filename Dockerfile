FROM public.ecr.aws/lambda/python:3.12

COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY src ${LAMBDA_TASK_ROOT}/src
RUN pip install --no-cache-dir ${LAMBDA_TASK_ROOT}

# Config and prompts ship in the image, so a deployed image pins both versions.
COPY config ${LAMBDA_TASK_ROOT}/config
COPY prompts ${LAMBDA_TASK_ROOT}/prompts

# Each function overrides CMD with its own handler (see infra/main.tf).
CMD ["bondhype.lambda_entry.scanner"]
