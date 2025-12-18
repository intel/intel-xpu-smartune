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


import os
import sys

import streamlit as st
import streamlit_antd_components as sac
from conf import VERSION
from pages.controller import apps_management, start_monitor_server, shutdown_monitor_server
from apis.api import Client_multiapps_api

# API 客户端初始化
api = Client_multiapps_api()

if 'language' not in st.session_state:
    st.session_state['language'] = 'English'  # default language setting

if "default_menu_index" not in st.session_state:
    st.session_state.default_menu_index = 0


def update_language_choice():
    st.session_state['language'] = st.session_state['selected_language']


if __name__ == "__main__":
    is_lite = "lite" in sys.argv

    try:
        st.set_page_config(
            page_icon="🧊",
            initial_sidebar_state="collapsed",  #  'expanded' or 'collapsed'
            layout="wide"
        )

        st.markdown("""
            <style>
                [data-testid="stSidebarNavLink"] {
                    display: none;
                }
                [data-testid="stSidebarNav"] {
                    display: none;
                }
            </style>
        """, unsafe_allow_html=True)

        st.markdown("""
            <style>
                .reportview-container {
                    margin-top: -2em;
                }
                #MainMenu {visibility: hidden;}
                header {visibility: hidden;}
                footer {visibility: hidden;}
                #stDecoration {display:none;}
            </style>
        """, unsafe_allow_html=True)

        if 'initialized' not in st.session_state:
            st.session_state['initialized'] = True
            api.start_client_callback()
            start_monitor_server()

        with st.sidebar:
            st.image(
                os.path.join(
                    "img",
                    "Intel-logo-48.png"
                )
            )
            st.markdown("<br>", unsafe_allow_html=True)

            st.markdown(f"""
            <div style="text-align: left; font-weight: bold; font-size: 24px;">
                Multi-Apps Manager
            </div>
            """, unsafe_allow_html=True)

            version_text = {
                "简体中文": "当前版本：",
                "English": "Current Version: "
            }
            st.caption(
                f"""<p align="left">{version_text[st.session_state['language']]}{VERSION}</p>""",
                unsafe_allow_html=True,
            )

            select_language_text = {
                'English': '🌐 Interface Display Language',
                '简体中文': '🌐 界面显示语言',
            }

            language_options = ["简体中文", "English"]

            selected_language = st.selectbox(
                select_language_text[st.session_state['language']],
                options=language_options,
                index=language_options.index(st.session_state.get('language', '简体中文')),
                on_change=update_language_choice,
                key='selected_language'
            )
            sac.divider(align='center', color='gray')

            menu_text = {
                "app_manager": {
                    "简体中文": "应用管理",
                    "English": "App Management",
                    "index": 0,
                    "func": apps_management
                }
            }
            menu_func = {}
            for k, v in menu_text.items():
                menu_func[v["index"]] = v["func"]

            # menu
            menu_index = sac.menu(
                items=[
                    sac.MenuItem(menu_text["app_manager"][selected_language], icon='gear')
                ],
                key='menu',
                index=st.session_state.default_menu_index,
                open_all=True,
                indent=20,
                format_func='title',
                return_index=True
            )

        if menu_index in menu_func and menu_func[menu_index]:
            menu_func[menu_index]()

    except KeyboardInterrupt:
        print("Ctrl+C detected, shutting down...")
        shutdown_monitor_server()
        sys.exit(0)  # Ensure the program exits cleanly
    except Exception as e:
        print(f"An error occurred: {e}")
        shutdown_monitor_server()
        sys.exit(1)
