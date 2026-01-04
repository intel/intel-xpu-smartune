#
#  Copyright (C) 2025 Intel Corporation
#
#  This software and the related documents are Intel copyrighted materials,
#  and your use of them is governed by the express license under which they
#  were provided to you ("License"). Unless the License provides otherwise,
#  you may not use, modify, copy, publish, distribute, disclose or transmit
#  his software or the related documents without Intel's prior written permission.
#
#  This software and the related documents are provided as is, with no express
#  or implied warranties, other than those that are expressly stated in the License.
#


import threading, time
from typing import Optional, Dict, Any

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from apis.api import Client_multiapps_api
from apis.api import callback_manager
from enum import Enum

# API 客户端初始化
api = Client_multiapps_api()

cb_running = False
callback_semaphore = threading.Semaphore(0)

controlled_apps = []

is_app_status_changed = False
is_app_resources_limited = False
is_app_manual_limit_by_user = False
is_high_usage_multiple_instances = False
current_app_name = ""


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
        st.session_state.controlled_apps_checked = False

    if "pending_apps" not in st.session_state:
        st.session_state.pending_apps = []

def get_all_apps():
    """获取所有可用应用"""
    apps = api.get_apps()
    return apps or []


# 共享数据结构和信号量
class CallbackData:
    def __init__(self):
        self.latest_data: Optional[Dict[str, Any]] = None
        self.lock = threading.Lock()
        self.data_ready = threading.Semaphore(0)  # 初始值为0的信号量
        self.has_new_data = False

callback_data = CallbackData()


# 回调处理器
def app_callback_handler(notify_data):
    """替换原来的队列操作"""
    print(f"App callback received: {notify_data}")
    try:
        with callback_data.lock:
            callback_data.latest_data = notify_data
            callback_data.has_new_data = True
        callback_data.data_ready.release()  # 释放信号量
    except Exception as e:
        print(f"Error in callback handler: {e}")


def register_notification():
    """主线程通知函数"""
    global is_app_status_changed, is_app_resources_limited, is_app_manual_limit_by_user, \
        is_high_usage_multiple_instances
    if is_app_resources_limited:
        st.toast(f'系统繁忙中，应用 {current_app_name} 被临时限制了资源使用，它将在系统资源空闲后适时恢复', icon='⚠️')
        is_app_resources_limited = False
    if is_app_status_changed:
        st.toast(f"应用 {current_app_name} 状态已更新", icon='ℹ️')
    if is_app_manual_limit_by_user:
        st.toast('系统繁忙中，检测到关键管控应用正在运行，建议您手动调整资源分配策略', icon='⚠️')
        is_app_manual_limit_by_user = False
    if is_high_usage_multiple_instances:
        st.toast('系统繁忙中, 可能系统运行的应用太多，建议优化应用部署数量', icon='⚠️')
        is_high_usage_multiple_instances = False


def _process_callback():
    """主线程安全的状态更新处理"""
    global is_app_status_changed, is_app_resources_limited, is_app_manual_limit_by_user, \
        current_app_name, is_high_usage_multiple_instances
    while cb_running:
        try:
            # 非阻塞检查是否有新数据
            if callback_data.data_ready.acquire(blocking=False):
                print("New callback data available.")
                with callback_data.lock:
                    data = callback_data.latest_data
                    callback_data.latest_data = None
                    callback_data.has_new_data = False

                if not data:
                    print("[WARNING] Empty callback data received")
                    continue

                app_id = data.get('app_id')
                app_name = data.get('app_name')
                new_status = data.get('status')
                purpose = data.get('purpose')

                print(f"Callback data: {data}")
                if not all([app_id, app_name, new_status, purpose]):
                    print(f"[ERROR] Incomplete callback data: {data}")
                    continue

                current_app_name = app_name
                if purpose == "app":
                    # 查找目标应用
                    for app in controlled_apps:
                        if app.get('app_id') == app_id or app.get('app_name') == app_name:
                            if app.get('status') != new_status:
                                app['status'] = new_status
                                is_app_status_changed = True
                                print(f"Status updated: {app_name} => {new_status}")
                            break

                    if new_status == "limited":
                        is_app_resources_limited = True

                if purpose == "notify":
                    if new_status == "manual_app_limit_by_user":
                        print(f"Notification: System busy, reminder user to limit app.")
                        is_app_manual_limit_by_user = True
                    if new_status == "high_usage_by_multiple_instances":
                        print(f"Notification: System busy, multiple instances consuming high resources.")
                        is_high_usage_multiple_instances = True
        except Exception as e:
            print(f"Error processing callback data: {e}")
        time.sleep(0.1)


def get_priority_color(priority):
    return {
        "critical": "#d00000",
        "high": "#ff4b4b",
        "medium": "#f4c20d",
        "low": "#34a853"
    }.get(priority.lower(), "#666666")


def app_management(default_apps):
    # 获取当前管控应用
    global controlled_apps, is_app_status_changed
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
            app_id, app_cmdline = next(
                ((app["app_id"], app["cmdline"]) for app in default_apps if app["name"] == selected_app),
                (None, "")
            )
            # print(f"Selected app_id: {app_id}, cmdline: {app_cmdline}")

        with cols[1]:
            priority = st.selectbox(
                "优先级",
                [p.value[0] for p in PriorityLevel],
                key="priority_select"
            )

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
                        "cmdline": app_cmdline,
                        "cgroup": "user"
                    })

                    if response:
                        st.session_state.controlled_apps.append({
                            "app_id": app_id,
                            "app_name": selected_app,
                            "priority": priority,
                            "oom_score": 0,
                            "controlled": True,
                            "cgroup": "user",
                            "remark": remark,
                            "cmdline": app_cmdline,
                            "status": "NA"
                        })
                        st.session_state.controlled_apps_checked = False
                        # st.rerun()
                    else:
                        st.error("添加管控失败")
                else:
                    st.error("未找到应用ID")

    st.divider()
    cols = st.columns([2, 5, 5, 3], gap='small')
    with cols[0]:
        st.subheader("管控列表")
    with cols[1]:
        with st.expander("Balancer功能说明：", expanded=True):
            st.markdown("""
            - 三大功能点：管控、监控和优先级队列
            - 管控：在系统资源紧张时，对占用资源最多的非管控或低优先级应用进行限制，释放系统资源，保障关键应用运行。
            - 监控：实时监控系统资源并进行评分，同时管理被管控应用的启动和关闭状态。
            - 优先级队列：当资源紧张时，自动暂停非关键应用的启动，将启动请求加入优先级队列，待资源充足后按优先级顺序自动启动。
            - 保活：对已设为关键的管控应用，启动时自动进入保活状态，确保其持续稳定运行。
            - 其他功能：支持用户手动管理管控应用，包括更改优先级、取消启动、设置资源限制、恢复正常、保活及删除等操作。
            """)
    with cols[2]:
        pending_queue_holder = st.empty()  # 占位容器

    with cols[3]:
        # if st.button("查看等待队列", key="pending_queue"):
        html_content = """
        <style>
        .task-card { padding: 1px; margin: 1px 0; border-radius: 5px; background: #f0f2f6; }
        .critical-priority { border-left: 5px solid #d00000; background-color: #ffe6e6; }
        .high-priority { border-left: 5px solid #ff4b4b; }
        .medium-priority { border-left: 5px solid #f4c20d; }
        .low-priority { border-left: 5px solid #34a853; }
        .priority-label {
            display: inline-block;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 0.8em;
            font-weight: bold;
            margin-left: 8px;
        }
        .empty-state { color: #666; font-style: italic; padding: 15px 0; text-align: center; }
        </style>
        """

        if is_app_status_changed:
            st.session_state.pending_apps = api.get_pending_apps()
            is_app_status_changed = False

        # print(f"pending_apps: {st.session_state.pending_apps}")

        if not st.session_state.pending_apps:
            html_content += '<div class="empty-state">🕊️ 等待队列为空</div>'
        else:
            priority_mapping = {
                "critical": "critical-priority",
                "high": "high-priority",
                "medium": "medium-priority",
                "low": "low-priority"
            }

            for app in st.session_state.pending_apps:
                priority = app.get("priority", "medium").lower()
                priority_class = priority_mapping.get(priority, "medium-priority")

                html_content += (
                    f'<div class="task-card {priority_class}">'
                    f'🔹 {app["app_name"]}  -  '
                    f'<span class="priority-label" style="color: {get_priority_color(priority)};">'
                    f'Priority: {app["priority"]}</span>'
                    '</div>'
                )

        pending_queue_holder.markdown(html_content, unsafe_allow_html=True)

    if not controlled_apps:
        st.info("当前没有管控的应用")
        return

    # print(f"Rendering controlled apps: {controlled_apps}")
    for idx, app in enumerate(controlled_apps):
        with st.container(border=True):
            cols = st.columns([2, 2, 1, 1, 1.5, 1.5, 1.5, 1, 1])  # 调整列宽比例
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

            with cols[3]:
                st.markdown(f"{status}")

            # 操作按钮
            with cols[4]:
                cancel_disabled = status != "pending"
                if st.button("⏹️ 取消启动", key=f"toggle_{app['app_id']}", type="primary", disabled=cancel_disabled):
                    # 从优先级队列中删除
                    cancel_result = api.cancel_relaunch(app["app_id"])
                    if cancel_result:
                        app["status"] = "canceled"
                        st.session_state.pending_apps = api.get_pending_apps()
                        print(f"App {app['app_name']} relaunch was canceled.")
                        st.toast(f"已取消自动启动应用{app['app_name']}!", icon='🎉')
                        # time.sleep(2)
                        # st.rerun()
                    else:
                        st.toast(f"取消自动启动应用{app['app_name']}失败!", icon="❌")

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
                    st.toast(f"已成功更新{app['app_name']}的优先级为{new_priority}!", icon='🎉')
                    # time.sleep(2)
                    # st.rerun()

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
                        limit_result = api.resource_limit(app["app_id"], app['app_name'], app["priority"])
                        if limit_result:
                            st.session_state[limit_key] = True
                            st.toast(f'已成功对应用{app["app_name"]}进行资源限制!', icon='🎉')
                        else:
                            st.toast(f'对应用{app["app_name"]}进行资源限制失败!', icon="❌")
                    # time.sleep(1)
                    # st.rerun()

            with cols[7]:
                priority = app.get("priority", "Medium").lower()
                if st.button("💪 保活", key=f"alive_{app['app_id']}", type="primary",
                             disabled=(priority != "critical" or status != "running")):
                    keep_alive_result = api.keep_alive_app(app["app_id"])
                    if keep_alive_result:
                        st.toast(f'已成功对应用{app["app_name"]}设置保活!', icon='🎉')
                        print(f"App {app['app_name']} set to keep alive.")
                    else:
                        st.toast(f'对应用{app["app_name"]}设置保活失败!', icon="❌")

            if cols[8].button("🗑️ 删除", key=f"delete_{app['app_id']}"):
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
                    # st.rerun()


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
    st.markdown(f"""
    <div style="text-align: center; font-weight: bold; font-size: 30px; margin-bottom: 30px;">
       Multi Apps Management
    </div>
    """, unsafe_allow_html=True)
    if "app_data" not in st.session_state or not st.session_state.app_data:
        st.session_state.app_data = get_all_apps()
    if not st.session_state.controlled_apps and not st.session_state.controlled_apps_checked:
        st.session_state.controlled_apps = api.get_controlled_apps() or []
        st.session_state.controlled_apps_checked = True

    if 'callback_registered' not in st.session_state:
        callback_manager.add_to_handler(app_callback_handler)
        st.session_state.callback_registered = True

    register_notification()
    app_management(st.session_state.app_data)
    # 增加页面自动刷新配合callback，以实现状态更新，实测不会影响用户操作
    st_autorefresh(interval=1000, key="autorefresh")
    _process_callback()


