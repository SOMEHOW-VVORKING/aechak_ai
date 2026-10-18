# 백엔드 레포의 Dockerfile은 자바 전용(MODULE build-arg + bootJar)이라 재사용할 수 없다.
#
# 빌더의 작업 경로를 런타임과 같게(/app) 둔다. venv는 재배치되지 않아서, 다른 경로에서 만들어
# 복사하면 스크립트 shebang이 사라진 인터프리터를 가리키고 "no such file or directory"로 죽는다.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# 의존성만 먼저 올려 레이어를 재사용한다. 락파일을 고정해 날마다 다른 버전이 들어오지 않게 한다.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project --frozen

COPY app ./app
COPY prompts ./prompts
RUN uv sync --no-dev --frozen


FROM python:3.12-slim AS runtime

# 루트로 돌리지 않는다
RUN useradd --create-home --uid 10001 aechak
WORKDIR /app

COPY --from=builder --chown=aechak:aechak /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # 추적 변수를 이미지 기본값으로 눌러 둔다. 하나만 켜져도 리뷰 본문이 외부로 나간다.
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false

USER aechak
EXPOSE 8000

# 백엔드 세 서비스의 타겟그룹은 /actuator/health 를 보지만 이 서비스는 /health 다.
# 전용 타겟그룹의 헬스체크 경로를 그렇게 잡아야 한다.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
