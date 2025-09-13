import sys
import uuid
import signal
import asyncio
from aioconsole import ainput
from config.config_loader import load_config
from config.logger import setup_logging
from core.utils.util import get_local_ip, validate_mcp_endpoint
from core.http_server import SimpleHttpServer
from core.xiaozhi_server_facade import XiaozhiServerFacade
from core.utils.util import check_ffmpeg_installed

TAG = __name__
logger = setup_logging()


async def wait_for_exit() -> None:
    """
    阻塞直到收到 Ctrl‑C / SIGTERM。
    - Unix: 使用 add_signal_handler
    - Windows: 依赖 KeyboardInterrupt
    """
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    if sys.platform != "win32":  # Unix / macOS
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        # Windows：await一个永远pending的fut，
        # 让 KeyboardInterrupt 冒泡到 asyncio.run，以此消除遗留普通线程导致进程退出阻塞的问题
        try:
            await asyncio.Future()
        except KeyboardInterrupt:  # Ctrl‑C
            pass


async def monitor_stdin():
    """监控标准输入，消费回车键"""
    while True:
        await ainput()  # 异步等待输入，消费回车


async def main():
    check_ffmpeg_installed()
    config = load_config()

    # 默认使用manager-api的secret作为auth_key
    # 如果secret为空，则生成随机密钥
    # auth_key用于jwt认证，比如视觉分析接口的jwt认证
    auth_key = config.get("manager-api", {}).get("secret", "")
    if not auth_key or len(auth_key) == 0 or "你" in auth_key:
        auth_key = str(uuid.uuid4().hex)
    config["server"]["auth_key"] = auth_key

    # 添加 stdin 监控任务
    stdin_task = asyncio.create_task(monitor_stdin())

    # 启动小智服务器门面（支持WebSocket和MQTT）
    xiaozhi_server = XiaozhiServerFacade(config)
    xiaozhi_task = asyncio.create_task(xiaozhi_server.start())
    
    # 启动 Simple http 服务器
    ota_server = SimpleHttpServer(config)
    ota_task = asyncio.create_task(ota_server.start())

    read_config_from_api = config.get("read_config_from_api", False)
    port = int(config["server"].get("http_port", 8003))
    if not read_config_from_api:
        logger.bind(tag=TAG).info(
            "OTA接口是\t\thttp://{}:{}/xiaozhi/ota/",
            get_local_ip(),
            port,
        )
    logger.bind(tag=TAG).info(
        "视觉分析接口是\thttp://{}:{}/mcp/vision/explain",
        get_local_ip(),
        port,
    )
    mcp_endpoint = config.get("mcp_endpoint", None)
    if mcp_endpoint is not None and "你" not in mcp_endpoint:
        # 校验MCP接入点格式
        if validate_mcp_endpoint(mcp_endpoint):
            logger.bind(tag=TAG).info("mcp接入点是\t{}", mcp_endpoint)
            # 将mcp计入点地址转成调用点
            mcp_endpoint = mcp_endpoint.replace("/mcp/", "/call/")
            config["mcp_endpoint"] = mcp_endpoint
        else:
            logger.bind(tag=TAG).error("mcp接入点不符合规范")
            config["mcp_endpoint"] = "你的接入点 websocket地址"

    # 显示协议连接信息
    connection_info = xiaozhi_server.get_connection_info()
    
    # WebSocket信息
    websocket_info = connection_info.get('websocket', {})
    if websocket_info.get('enabled', False):
        websocket_port = websocket_info.get('port', 8000)
        logger.bind(tag=TAG).info(
            "WebSocket地址是\tws://{}:{}/xiaozhi/v1/",
            get_local_ip(),
            websocket_port,
        )
    
    # MQTT信息
    mqtt_info = connection_info.get('mqtt', {})
    if mqtt_info.get('enabled', False):
        mqtt_port = mqtt_info.get('port', 1883)
        udp_port = mqtt_info.get('udp_port', 1883)
        logger.bind(tag=TAG).info(
            "MQTT地址是\t\tmqtt://{}:{}",
            get_local_ip(),
            mqtt_port,
        )
        logger.bind(tag=TAG).info(
            "UDP音频端口是\t{}:{}",
            get_local_ip(),
            udp_port,
        )
    
    # 显示启用的协议
    enabled_protocols = xiaozhi_server.config.get('enabled_protocols', [])
    logger.bind(tag=TAG).info(f"启用的协议: {', '.join(enabled_protocols)}")
    
    if 'websocket' in enabled_protocols:
        logger.bind(tag=TAG).info(
            "=======上面的WebSocket地址请勿用浏览器访问======="
        )
        logger.bind(tag=TAG).info(
            "如想测试WebSocket请用谷歌浏览器打开test目录下的test_page.html"
        )
    
    if 'mqtt' in enabled_protocols:
        logger.bind(tag=TAG).info(
            "=======MQTT客户端ID格式: GID_test@@@mac_address@@@uuid======="
        )
    
    logger.bind(tag=TAG).info(
        "=============================================================\n"
    )

    try:
        await wait_for_exit()  # 阻塞直到收到退出信号
    except asyncio.CancelledError:
        print("任务被取消，清理资源中...")
    finally:
        # 停止小智服务器
        try:
            await xiaozhi_server.stop()
        except Exception as e:
            logger.error(f"停止小智服务器失败: {e}")
        
        # 取消所有任务（关键修复点）
        stdin_task.cancel()
        xiaozhi_task.cancel()
        if ota_task:
            ota_task.cancel()

        # 等待任务终止（必须加超时）
        tasks_to_wait = [stdin_task, xiaozhi_task]
        if ota_task:
            tasks_to_wait.append(ota_task)
            
        await asyncio.wait(
            tasks_to_wait,
            timeout=3.0,
            return_when=asyncio.ALL_COMPLETED,
        )
        print("服务器已关闭，程序退出。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("手动中断，程序终止。")
