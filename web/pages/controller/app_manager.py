import streamlit as st
from apis.api import Client_multiapps_api
from apis.api import callback_manager
from enum import Enum

# API 客户端初始化
api = Client_multiapps_api()


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


def app_callback_handler(notify_data):
    """处理来自多应用启动服务的回调"""
    print(f"App callback received: {notify_data}")


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
                            "remark": remark
                        })
                        callback_manager.add_to_handler(app_callback_handler)

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

    for idx, app in enumerate(controlled_apps):
        with st.container(border=True):
            cols = st.columns([2, 1.5, 1.5, 1, 1, 1, 1, 1])  # 调整列宽比例
            status = app.get("status", "stopped")

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

            # 状态（模拟）
            cols[3].write(app.get("status", "stopped"))

            # 操作按钮
            with cols[4]:
                if status == "running":
                    if st.button("⏯️ 取消启动", key=f"toggle_{app['app_id']}", type="primary"):
                        # 从优先级队列中删除
                        pass

            if cols[5].button("🔧 更新优先级", key=f"priority_{idx}", type="primary"):
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
                    st.rerun()

            with cols[6]:
                if status == "running":
                    if st.button("🔧 资源限制", key=f"limit_{idx}", type="primary"):
                        pass

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


def apps_management():
    init()
    st.markdown(f"""
    <div style="text-align: center; font-weight: bold; font-size: 30px; margin-bottom: 30px;">
       Multi Apps Management
    </div>
    """, unsafe_allow_html=True)
    if "app_data" not in st.session_state or not st.session_state.app_data:
        st.session_state.app_data = get_all_apps()
    if not st.session_state.controlled_apps:
        st.session_state.controlled_apps = api.get_controlled_apps() or []
    app_management(st.session_state.app_data)
