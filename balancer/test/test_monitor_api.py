# Copyright (c) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0


import os, requests, json

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

_TEST_HOST = "127.0.0.1"
_TEST_PORT = 9001
_BASE_URL = f"https://{_TEST_HOST}:{_TEST_PORT}/monitor"

B_CERT_FILE = "../../key/b_server.crt"

def main():
    def _create_session():
        """Create a requests session with retry strategy and SSL configuration."""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST", "GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)

        if B_CERT_FILE and os.path.exists(B_CERT_FILE):
            session.verify = B_CERT_FILE
            print(f"Using custom certificate for SSL verification: {B_CERT_FILE}")
        else:
            print(f"Warning: Certificate file {B_CERT_FILE} not found. SSL verification is disabled.")
            session.verify = False
            # Disable ssl
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        return session


    def _test_endpoint(path: str) -> None:
        url = f"{_BASE_URL}{path}"
        session = _create_session()
        print(f"\n{'=' * 60}")
        print(f"GET {url}")
        print('=' * 60)
        try:
            resp = session.get(url, timeout=10)
            resp.raise_for_status()

            print(f"Status : {resp.status_code}")
            print("Response:")
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        except requests.exceptions.SSLError as e:
            print(f"SSL verification failed: {e}, please check your SSL configuration.")
        except Exception as exc:
            print(f"Request failed: {exc}")

    _test_endpoint("/cpu")
    _test_endpoint("/memory")
    _test_endpoint("/disk")
    _test_endpoint("/network")
    _test_endpoint("/pressure")
    _test_endpoint("/summary")
    _test_endpoint("/top_consumers")
    _test_endpoint("/app_resource_stats")
    _test_endpoint("/app_disk_io_stats")

    print(f"\n{'=' * 60}")
    print("All monitor API tests completed.")
    print('=' * 60)


if __name__ == "__main__":
    main()

