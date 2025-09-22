import queue
import threading, time
from typing import Optional, Dict, Any

import streamlit as st

from apis.api import Client_multiapps_api
from apis.api import callback_manager
from enum import Enum

# API 客户端初始化
api = Client_multiapps_api()

cb_running = False
callback_queue = queue.Queue()
# new_callback_event = threading.Event()

# 优先级枚举定义 (放在模块顶部)
class PriorityLevel(Enum):
    LOW = ("Low", 1)
    MEDIUM = ("Medium", 2)
    HIGH = ("High", 3)
    CRITICAL = ("Critical", 4)


# 优先级颜色映射
PRIORITY_COLORS = {
    "Critical": "red",
    "High": "orange",
    "Medium": "blue",
    "Low": "green"
}


def init():
    """初始化会话状态"""
    if "app_data" not in st.session_state:
        st.session_state.app_data = {}

    if "controlled_apps" not in st.session_state:
        st.session_state.controlled_apps = []


def get_all_apps():
    """获取所有可用应用"""
    apps = api.get_apps()
    # print(f"Retrieved apps: {apps}")
    return apps or []


# ---------- 替换原来的 queue.Queue ----------
class ThreadSafeCallback:
    _lock = threading.Lock()
    _latest_data: Optional[Dict[str, Any]] = None
    _has_new_data: bool = False

    @classmethod
    def put(cls, data: Dict[str, Any]):
        """线程安全的回调数据存储"""
        with cls._lock:
            cls._latest_data = data
            cls._has_new_data = True
            print(f"[Callback Stored] {data}")
    @classmethod
    def get(cls) -> Optional[Dict[str, Any]]:
        """主线程安全获取数据"""
        with cls._lock:
            if not cls._has_new_data:
                return None
            cls._has_new_data = False
            return cls._latest_data

# ---------- 修改原回调处理器 ----------
def app_callback_handler(notify_data):
    """替换原来的队列操作"""
    global callback_queue
    print(f"App callback received: {notify_data}")
    try:
        callback_queue.put(notify_data)
        # new_callback_event.set()
    except Exception as e:
        print(f"Error in callback handler: {e}")


# 全局状态锁
status_lock = threading.Lock()


def _process_callback():
    """主线程安全的状态更新处理"""
    try:
        print("Processing callback data...")

        while cb_running:
            if not callback_queue.empty():
                data = callback_queue.get_nowait()

                app_id = data.get('app_id')
                app_name = data.get('app_name')
                new_status = data.get('status')
                purpose = data.get('purpose')

                if not all([app_id, app_name, new_status, purpose]):
                    continue

                with status_lock:
                    updated = False

                    # 查找目标应用
                    for app in st.session_state.controlled_apps:
                        if app.get('app_id') == app_id or app.get('app_name') == app_name:
                            if app.get('status') != new_status:
                                app['status'] = new_status
                                updated = True
                                print(f"Status updated: {app_name} => {new_status}")
                            break

                    if updated:
                        if purpose == "notify":
                            st.toast(
                                f'系统繁忙中，管控的应用 {app_name} 被自动限制了资源使用，它将在系统资源空闲后自动恢复',
                                icon='⚠️')
                            time.sleep(2)
                        st.rerun()

                if purpose == "notify":
                    print(f"Notification: System busy, controlled app {app_name} limited.")
                    st.toast(f'系统繁忙中，非管控应用 {app_name} 被自动限制了资源使用，它将在系统资源空闲后自动恢复', icon='⚠️')
            else:
                time.sleep(1)

    except Exception as e:
        print(f"Error processing callback data: {e}")


def app_management(default_apps):
    # 获取当前管控应用
    controlled_apps = st.session_state.controlled_apps

    # 添加应用管控区域
    with st.container(border=True):
        st.markdown("""
        <div style="text-align: left; font-weight: bold; font-size: 18px;">
            添加应用管控
        </div>
        """, unsafe_allow_html=True)

        cols = st.columns([1, 1], gap='small')
        with cols[0]:
            selected_app = st.selectbox(
                "选择应用",
                [app["name"] for app in default_apps],
                key="app_select"
            )
            app_id = next((app["app_id"] for app in default_apps if app["name"] == selected_app), None)
            print(f"Selected app_id: {app_id}")

        with cols[1]:
            priority = st.selectbox(
                "优先级",
                [p.value[0] for p in PriorityLevel],
                key="priority_select"
            )
            print(f"Selected priority: {priority}")

        remark = st.text_input("备注", key="remark_input")

        cols = st.columns([5, 1, 5], gap='small')
        with cols[1]:
            if st.button(" 👇 添加", key="add_control", type="primary"):
                if app_id:
                    response = api.set_controlled_apps({
                        "app_id": app_id,
                        "app_name": selected_app,
                        "priority": priority,
                        "controlled": True,
                        "remark": remark,
                        "cgroup": "user"
                    })

                    if response:
                        st.session_state.controlled_apps.append({
                            "app_id": app_id,
                            "app_name": selected_app,
                            "priority": priority,
                            "controlled": True,
                            "cgroup": "user",
                            "remark": remark,
                            "status": "NA"
                        })
                        st.rerun()
                    else:
                        st.error("添加管控失败")
                else:
                    st.error("未找到应用ID")

    st.divider()
    st.subheader("管控列表")

    if not controlled_apps:
        st.info("当前没有管控的应用")
        return

    print(f"Rendering controlled apps: {controlled_apps}")
    for idx, app in enumerate(controlled_apps):
        with st.container(border=True):
            cols = st.columns([2, 1.5, 1.5, 1, 1, 1, 1, 1])  # 调整列宽比例
            status = app.get("status", "NA")

            # 应用名称
            cols[0].write(f"**👁️ {app['app_name']}**")

            # 优先级显示和修改
            with cols[1]:
                current_priority = app.get("priority", "Medium")
                new_priority = st.selectbox(
                    "优先级",
                    [p.value[0] for p in PriorityLevel],
                    index=[p.value[0] for p in PriorityLevel].index(current_priority),
                    key=f"priority_{app['app_id']}",
                    label_visibility="collapsed"
                )

                # 颜色高亮
                st.markdown(
                    f"<span style='color: {PRIORITY_COLORS[new_priority]};'>■</span>",
                    unsafe_allow_html=True
                )

            # 所属CGroup
            cols[2].write(app.get("cgroup", "user"))

            # 状态
            print(f"App {app['app_name']} status: {status}")
            with cols[3]:
                st.write(f"{status}")

            # 操作按钮
            with cols[4]:
                cancel_disabled = status != "pending"
                if st.button("⏹️ 取消启动", key=f"toggle_{app['app_id']}", type="primary", disabled=cancel_disabled):
                    # 从优先级队列中删除
                    cancel_result = api.cancel_relaunch(app["app_id"])
                    if cancel_result:
                        app["status"] = "canceled"
                        print(f"App {app['app_name']} relaunch was canceled.")
                        st.toast(f'已取消自动启动应用{app['app_name']}!', icon='🎉')
                        time.sleep(2)
                        st.rerun()
                    else:
                        st.toast(f'取消自动启动应用{app['app_name']}失败!', icon="❌")

            if cols[5].button("📊 更新优先级", key=f"priority_{idx}", type="primary"):
                if new_priority != current_priority:
                    api.set_priority({
                        "app_id": app["app_id"],
                        "priority": new_priority
                    })
                    for c_app in st.session_state.controlled_apps:
                        if c_app["app_id"] == app["app_id"]:
                            c_app["priority"] = new_priority
                            break
                    print(f"update priority: st.session_state.controlled_apps: {st.session_state.controlled_apps}")
                    st.toast(f'已成功更新{app['app_name']}的优先级为{new_priority}!', icon='🎉')
                    time.sleep(2)
                    st.rerun()

            with cols[6]:
                limit_key = f"resource_limit_{app['app_id']}"
                if limit_key not in st.session_state:
                    st.session_state[limit_key] = False  # False表示"限制"状态，True表示"恢复"状态

                btn_text = "🔄 恢复正常" if st.session_state[limit_key] else "⛔ 资源限制"
                btn_type = "secondary" if st.session_state[limit_key] else "primary"

                limit_disabled = (status != "running") and not st.session_state[limit_key]

                if st.button(btn_text, key=f"limit_{idx}", type=btn_type, disabled=limit_disabled):
                    if st.session_state[limit_key]:
                        restore_result = api.restore_resource(app["app_id"])
                        if restore_result:
                            st.session_state[limit_key] = False
                            st.toast(f'已成功恢复应用{app["app_name"]}的资源!', icon='🎉')
                        else:
                            st.toast(f'恢复应用{app["app_name"]}的资源失败!', icon="❌")
                    else:
                        limit_result = api.resource_limit(app["app_id"])
                        if limit_result:
                            st.session_state[limit_key] = True
                            st.toast(f'已成功对应用{app["app_name"]}进行资源限制!', icon='🎉')
                        else:
                            st.toast(f'对应用{app["app_name"]}进行资源限制失败!', icon="❌")
                    time.sleep(1)
                    st.rerun()

            if cols[7].button("🗑️ 删除", key=f"delete_{app['app_id']}"):
                response = api.remove_controlled_apps({
                    "app_id": app["app_id"],
                    "app_name": app["app_name"]
                })
                if response:
                    st.session_state.controlled_apps = [
                        c_app for c_app in st.session_state.controlled_apps
                        if c_app["app_id"] != app["app_id"]
                    ]
                    print(f"remove: st.session_state.controlled_apps: {st.session_state.controlled_apps}")
                    st.rerun()


def start_monitor_server():
    print("Starting monitor server to obtain notification...")
    global cb_running
    if cb_running:
        print("服务已经在运行，无需再次启动")
        return

    cb_running = True


def shutdown_monitor_server():
    print(f"Received signal, exiting...")
    global cb_running
    if not cb_running:
        print("服务已经停止，无需再次操作")
        return
    cb_running = False


def apps_management():
    init()
    print("Starting apps management...")
    st.markdown(f"""
    <div style="text-align: center; font-weight: bold; font-size: 30px; margin-bottom: 30px;">
       Multi Apps Management
    </div>
    """, unsafe_allow_html=True)
    if "app_data" not in st.session_state or not st.session_state.app_data:
        st.session_state.app_data = get_all_apps()
    if not st.session_state.controlled_apps:
        st.session_state.controlled_apps = api.get_controlled_apps() or []

    if 'callback_registered' not in st.session_state:
        callback_manager.add_to_handler(app_callback_handler)
        st.session_state.callback_registered = True

    # if callback_data := ThreadSafeCallback.get():  # 安全获取最新回调
    #     _process_callback_data(callback_data)
    print("Rendering app management UI...")
    app_management(st.session_state.app_data)
    _process_callback()
    print("App management UI rendered.")
