"""双人播客 WebUI 后端（模块化入口，向后兼容的薄壳）。

原 1647 行单文件已拆分到 app/ 包：
  app/config.py        启动参数、目录、共享 HTTP 客户端
  app/models.py        Pydantic 模型
  app/stores.py        项目/术语表存储
  app/queue_state.py   队列状态与持久化
  app/queue_worker.py  队列执行器（提交/轮询/断连容错）
  app/routes/*         全部 HTTP 端点
  app/main.py          FastAPI 应用与启动恢复

启动方式（不变）：
  python server.py --tts-url http://gpu-server:8000 --host 0.0.0.0 --port 3001
"""

from app.main import app, run  # noqa: F401

if __name__ == "__main__":
    run()
