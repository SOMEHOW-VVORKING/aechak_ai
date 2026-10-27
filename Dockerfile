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

# 포트를 **환경변수 하나로** 둔다. 같은 이미지가 두 서비스를 띄우기 때문이다 --
# 검수 컨슈머(app.main)와 초안 API(app.draft_api.app)는 CMD 와 포트만 다르다.
#
# 전에는 CMD 와 HEALTHCHECK 가 8000 을 **각자 박아** 두었다. 그러면 초안 서비스를
# PORT 8200 으로 띄울 때 컨테이너는 8200 을 듣는데 Docker 헬스체크는 8000 을 찔러
# **영구 unhealthy** 가 되고 ECS 가 태스크를 무한 교체한다. 초안 앱에도 /health 가
# 있으므로 포트만 맞으면 되는 문제였고, 그 포트가 이미지에 구워져 있던 것이다.
ENV PORT=8000

USER aechak
EXPOSE 8000

# 백엔드 세 서비스의 타겟그룹은 /actuator/health 를 보지만 이 서비스는 /health 다.
# 전용 타겟그룹의 헬스체크 경로를 그렇게 잡아야 한다.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').status==200 else 1)"

# ``sh -c`` 로 두어야 ``$PORT`` 가 치환된다. exec 형식은 변수를 펼치지 않는다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
