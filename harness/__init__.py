"""측정 하네스.

레포 루트에 두고 ``tests/`` 밑에 두지 않는다. pyproject의 ``testpaths``가 ``tests``라서
그 안에 두면 pytest가 수집해 CI에서 실제 모델 호출이 돈다. Dockerfile도 ``app``과
``prompts``만 COPY하므로 이미지에는 들어가지 않는다.

``python -m harness.run`` 으로 돈다.
"""
