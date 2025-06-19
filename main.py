import os
import io
import sys
import base64
import threading
import keyboard
import requests
import configparser
import socket
import time
import platform
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO
from mss import mss
from engineio.async_drivers import gevent
from PIL import Image
from openai import OpenAI
from win10toast import ToastNotifier

# ==== 路径工具 ====
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, 'config.ini')

# ==== 通知工具 ====
notifier = ToastNotifier()

def show_notification(message, title="ExamHelper通知", is_error=False, is_success=False):
    try:
        notifier.show_toast(
            title if not is_success else "成功",
            message,
            icon_path=os.path.join(STATIC_DIR, "icon.ico"),
            duration=5,
            threaded=True
        )
    except Exception as e:
        print(f"[通知失败] {e}")

# ==== 配置读取 ====
def load_config():
    config = configparser.ConfigParser()
    defaults = {
        'model': 'doubao-seed-1-6-250615',
        'base_url': 'https://ark.cn-beijing.volces.com/api/v3',
        'api_key': '',
        'prompt': '请识别图中的内容并进行思考和回答，请尽量精简。',
        'port': '5678',
        'recognition_hotkey': 'space',
        'interruption_hotkey': 'ctrl',
        'exit_hotkey': 'ctrl+q'
    }

    if not os.path.exists(CONFIG_PATH):
        show_notification(f"未找到 config.ini，将在 {CONFIG_PATH} 创建默认配置")
        config['OpenAI'] = {
            'model': defaults['model'],
            'base_url': defaults['base_url'],
            'api_key': defaults['api_key'],
            'prompt': defaults['prompt']
        }
        config['Flask'] = {'port': defaults['port']}
        config['Hotkeys'] = {
            'recognition': defaults['recognition_hotkey'],
            'interruption': defaults['interruption_hotkey'],
            'exit': defaults['exit_hotkey']
        }
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            config.write(f)
        show_notification("已创建默认配置文件 config.ini", is_success=True)

    config.read(CONFIG_PATH, encoding='utf-8')
    return {
        'model': config.get('OpenAI', 'model', fallback=defaults['model']),
        'base_url': config.get('OpenAI', 'base_url', fallback=defaults['base_url']),
        'api_key': config.get('OpenAI', 'api_key', fallback=defaults['api_key']),
        'prompt': config.get('OpenAI', 'prompt', fallback=defaults['prompt']),
        'port': config.getint('Flask', 'port', fallback=int(defaults['port'])),
        'recognition_hotkey': config.get('Hotkeys', 'recognition', fallback=defaults['recognition_hotkey']),
        'interruption_hotkey': config.get('Hotkeys', 'interruption', fallback=defaults['interruption_hotkey']),
        'exit_hotkey': config.get('Hotkeys', 'exit', fallback=defaults['exit_hotkey'])
    }

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

config = load_config()
if not config['api_key']:
    show_notification("程序将结束 原因：API Key 未设置，请编辑 config.ini", is_error=True)
    time.sleep(0.5)
    os._exit(0)

should_stop = False
recognition_lock = threading.Lock()

client = OpenAI(base_url=config['base_url'], api_key=config['api_key'])
app = Flask(__name__, static_folder=STATIC_DIR)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

html_template = f"""
<!doctype html>
<html>
  <head>
    <title>ExamHelper</title>
    <meta charset="utf-8">
    <link rel="icon" href="/static/icon.ico" sizes="32x32"/>
    <style>
      body {{ font-family: sans-serif; margin: 2em; background: #f8f9fa; font-size: 35px; }}
      #output {{ white-space: pre-wrap; padding-bottom: 200px; }}
    </style>
    <script src="/static/marked.min.js"></script>
    <script src="/static/socket.io.min.js"></script>
  </head>
  <body>
    <h2><img src="/static/icon.ico" style="height:1em;vertical-align:middle"> ExamHelper</h2>
    <div id="output">等待回答...</div>
    <script>
      const socket = io();
      let latestContent = "";
      let shouldUpdate = false;
      socket.on("response", (data) => {{ latestContent = data; shouldUpdate = true; }});
      socket.on("clear", (message) => {{ latestContent = message || "等待回答..."; shouldUpdate = true; }});
      setInterval(() => {{
        if (shouldUpdate) {{
          document.getElementById("output").innerHTML = marked.parse(latestContent);
          window.scrollTo(0, document.body.scrollHeight);
          shouldUpdate = false;
        }}
      }}, 150);
    </script>
  </body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(html_template)

@app.route("/submit", methods=["POST"])
def submit():
    socketio.emit("response", request.json.get("text", ""))
    return "ok"

@app.route("/clear", methods=["POST"])
def clear():
    socketio.emit("clear", request.json.get("message", "等待回答..."))
    return "cleared"

def get_image_base64():
    with mss() as sct:
        img = Image.frombytes("RGB", sct.grab(sct.monitors[1]).size, sct.grab(sct.monitors[1]).rgb)
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()

def ask_openai_stream(image_b64):
    global should_stop
    try:
        with recognition_lock:
            should_stop = False
            messages = [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": config['prompt']},
                ]
            }]
            response = client.chat.completions.create(
                model=config['model'], messages=messages, max_tokens=10000, stream=True)
            reasoning_buffer, answer_buffer = "", ""
            for chunk in response:
                if should_stop: break
                delta = chunk.choices[0].delta if chunk.choices and chunk.choices[0].delta else None
                if not delta: continue
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    reasoning_buffer += delta.reasoning_content
                if hasattr(delta, "content") and delta.content:
                    answer_buffer += delta.content
                full = ""
                if reasoning_buffer:
                    full += f"### 🧩 思考中...\n\n{reasoning_buffer}\n\n"
                if answer_buffer:
                    full += f"### 📝 回答：\n\n{answer_buffer}"
                socketio.emit("response", full)
                time.sleep(0.01)
    except Exception as e:
        socketio.emit("response", f"❌ 识别失败: {str(e)}")
    finally:
        should_stop = False

def on_recognition():
    print("[识别] 快捷键触发，截图并识别...")
    requests.post(f"http://localhost:{config['port']}/clear", json={"message": "🖼️ 已截图，正在连接 AI 识别中..."})
    image_b64 = get_image_base64()
    threading.Thread(target=ask_openai_stream, args=(image_b64,), daemon=True).start()

def stop_recognition():
    global should_stop
    should_stop = True
    print("[中断] 请求中断识别")

def exit_program():
    show_notification("退出快捷键触发，程序将退出。", is_success=True)
    time.sleep(0.5)
    os._exit(0)

def keyboard_listener():
    keyboard.add_hotkey(config['recognition_hotkey'], on_recognition)
    keyboard.add_hotkey(config['interruption_hotkey'], stop_recognition)
    keyboard.add_hotkey(config['exit_hotkey'], exit_program)
    keyboard.wait()

def start_server():
    socketio.run(app, host="0.0.0.0", port=config['port'], allow_unsafe_werkzeug=True)

def main():
    threading.Thread(target=keyboard_listener, daemon=True).start()
    if platform.system() == "Windows":
        import ctypes
        ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    time.sleep(1)
    ip = get_local_ip()
    show_notification(f"服务器启动成功，请访问 http://{ip}:{config['port']}", is_success=True)
    server_thread.join()

if __name__ == "__main__":
    main()