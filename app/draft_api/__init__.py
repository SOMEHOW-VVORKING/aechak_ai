"""초안 전용 ASGI 앱. **운영 앱(``app/main.py``)의 라우트 집합을 건드리지 않는다.**

그 파일 머리글이 결정을 이미 적어 뒀다 -- "HTTP 로는 헬스체크만 공개한다. 판정은 전부
카프카로 들어오고, **공개 ALB 뒤에 있으므로 관리용 경로를 열어 두면 이름이 internal 이어도
외부에서 닿는다.**" 그 문서화된 보안 판단을 숫자 없이 뒤집지 않는다.

``demo/app.py`` 가 쓴 "별 ASGI 앱" 패턴을 그대로 쓰되 ``app/`` 아래 둔다 -- 운영 코드이므로
Dockerfile 의 ``COPY app ./app`` 에 들어가야 한다. 같은 이미지, 다른 커맨드, 다른 포트,
다른 ECS 서비스다.

    uv run uvicorn app.draft_api.app:app --port 8200

컨테이너로 띄울 때는 **``PORT`` 와 ``CMD`` 를 같이 준다.** Dockerfile 의 ``CMD`` 와
``HEALTHCHECK`` 가 둘 다 ``$PORT`` 를 읽으므로 그 변수 하나만 맞추면 둘이 같이 움직인다.
전에는 ``HEALTHCHECK`` 가 8000 을 박아 두어, 초안 서비스를 8200 으로 띄우면 컨테이너는
8200 을 듣는데 헬스체크는 8000 을 찔러 **영구 unhealthy** 가 됐다.

    docker run -e PORT=8200 <image> \
        sh -c 'exec uvicorn app.draft_api.app:app --host 0.0.0.0 --port ${PORT}'
"""
