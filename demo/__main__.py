"""``python -m demo`` 로 콘솔을 띄운다.

**127.0.0.1 에만 바인딩한다.** 인증이 없는 개발·데모용 화면이라 바깥에 내놓지 않는다.
운영 앱은 다른 프로세스(포트 8000)이고 이 모듈과 아무 관계가 없다.
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(prog="demo", description="검수 단계 데모 콘솔")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--host", default="127.0.0.1", help="기본값은 로컬 전용이다")
    args = parser.parse_args()
    uvicorn.run("demo.app:app", host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
