街区诉求回应与治理分析服务。依赖需在离线运行前按 requirements.txt 预装。
启动 API：uvicorn civic_flow.api:app --host 127.0.0.1 --port 8000
运行测试：python -B -m unittest discover -s tests -v
源码检查：python -B -m civic_flow.check_source
本地验收：python -B -m civic_flow.demo
