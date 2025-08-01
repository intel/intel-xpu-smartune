# test.py
import requests
import json

BASE_URL = "http://localhost:9001"


def test_add_workload():
    payloads = [
        {
            "priority": "critical",
            "payload": {"pid": 12345, "task": "high_priority_service"}
        },
        # {
        #     "priority": "normal",
        #     "payload": {"pid": 23456, "task": "regular_service"}
        # }
    ]

    for data in payloads:
        try:
            resp = requests.post(
                f"{BASE_URL}/add_workload",
                json=data,
                timeout=3
            )
            print(f"Response: {resp.status_code} - {resp.json()}")
        except requests.exceptions.RequestException as e:
            print(f"Request failed: {str(e)}")


def test_get_apps():
    """测试获取应用列表API"""
    try:
        resp = requests.get(
            f"{BASE_URL}/app/get_apps",
            timeout=5
        )
        print("\n[GET /app/get_apps]")
        print(f"Status: {resp.status_code}")
        print("Response:")
        print(json.dumps(resp.json(), indent=2))

        # 验证基本响应结构
        data = resp.json()
        assert isinstance(data.get("data", []), list), "Response data should be a list"
        print("✓ Valid response structure")
    except Exception as e:
        print(f"Test failed: {str(e)}")


def test_set_priority():
    """测试设置优先级API"""
    test_cases = [
        {
            "name": "正常设置优先级",
            "payload": {"app_id": "gnome-privacy-panel.desktop", "priority": "high", "cgroup": "system"},
            "expected": "high"
        },
        {
            "name": "设置不存在的应用",
            "payload": {"app_id": "nonexistent.app", "priority": "critical", "cgroup": "user"},
            "expected": None  # 预期会失败
        }
    ]

    for case in test_cases:
        try:
            print(f"\n[POST /app/set_priority] {case['name']}")
            resp = requests.post(
                f"{BASE_URL}/app/set_priority",
                json=case["payload"],
                timeout=5
            )
            print(f"Status: {resp.status_code}")
            print("Response:")
            print(json.dumps(resp.json(), indent=2))

            if case["expected"]:
                assert resp.json().get("code", 1) == 0, "Expected success response"
                print(f"✓ Priority set to {case['expected']}")
        except Exception as e:
            print(f"Test case failed: {str(e)}")


def test_get_priority():
    """测试根据app_id获取优先级设置API"""
    try:
        print("\n[POST /app/get_priority]")
        test_app_id = "gnome-privacy-panel.desktop" # "gnome-system-panel.desktop"

        resp = requests.post(
            f"{BASE_URL}/app/get_priority",
            json={"app_id": test_app_id},
            timeout=5
        )

        print(f"Status: {resp.status_code}")
        print("Response:")
        print(json.dumps(resp.json(), indent=2))

        # 检查响应数据
        data = resp.json()
        if resp.status_code == 200:
            if data.get("data") and data["data"]["app_id"] == test_app_id:
                print(f"✓ Found priority for {test_app_id}: {data['data']['priority']}")
            else:
                print(f"✗ Priority data for {test_app_id} not found in response")
        elif resp.status_code == 404:
            print(f"✗ App {test_app_id} not found")
        else:
            print(f"✗ Unexpected response status: {resp.status_code}")

    except Exception as e:
        print(f"Test failed: {str(e)}")


if __name__ == "__main__":
    # test_add_workload()
    # test_get_apps()
    # test_set_priority()
    test_get_priority()
