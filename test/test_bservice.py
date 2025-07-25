# test.py
import requests

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


if __name__ == "__main__":
    test_add_workload()
