import os
import select

class PSIEventMonitor:
    CPU_PRESSURE_FILE = "/proc/pressure/cpu"
    MEMORY_PRESSURE_FILE = "/proc/pressure/memory"  # 新增
    IO_PRESSURE_FILE = "/proc/pressure/io"
    CPU_TRIGGER_THRESHOLD_MS = 100  # 100ms
    MEMORY_TRIGGER_THRESHOLD_MS = 100  # 新增 100ms
    IO_TRIGGER_THRESHOLD_MS = 100   # 100ms
    TRACKING_WINDOW_SECS = 1        # 1秒窗口

    def __init__(self):
        self.fds = {}
        self.cpu_event_count = 0
        self.memory_event_count = 0  # 新增
        self.io_event_count = 0

    def _setup_trigger(self, fd, threshold_ms, window_secs):
        """设置触发条件并验证"""
        trigger = f"some {threshold_ms * 1000} {window_secs * 1000000}\n"
        os.write(fd, trigger.encode())
        os.lseek(fd, 0, os.SEEK_SET)
        current_trigger = os.read(fd, 1024).decode()
        print(f"Set trigger: {trigger.strip()} | Current: {current_trigger.strip()}")

    def setup_polling(self):
        """初始化文件描述符和触发器"""
        try:
            print("Opening PSI files...")
            self.fds['cpu'] = os.open(self.CPU_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK)
            self.fds['memory'] = os.open(self.MEMORY_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK)  # 新增
            self.fds['io'] = os.open(self.IO_PRESSURE_FILE, os.O_RDWR | os.O_NONBLOCK)
            print(f"FDs: CPU={self.fds['cpu']}, Memory={self.fds['memory']}, IO={self.fds['io']}")  # 修改

            self._setup_trigger(self.fds['cpu'], self.CPU_TRIGGER_THRESHOLD_MS, self.TRACKING_WINDOW_SECS)
            self._setup_trigger(self.fds['memory'], self.MEMORY_TRIGGER_THRESHOLD_MS, self.TRACKING_WINDOW_SECS)  # 新增
            self._setup_trigger(self.fds['io'], self.IO_TRIGGER_THRESHOLD_MS, self.TRACKING_WINDOW_SECS)
        except Exception as e:
            print(f"Setup failed: {e}")
            self.cleanup()
            raise

    def wait_for_events(self):
        """监听并处理事件"""
        poller = select.poll()
        for fd in self.fds.values():
            poller.register(fd, select.POLLPRI)
        print("Listening for events...")

        while True:
            try:
                events = poller.poll()
                print(f"Poll result: {events}")
                for fd, event in events:
                    os.lseek(fd, 0, os.SEEK_SET)
                    data = os.read(fd, 1024).decode()
                    print(f"Event data: {data.strip()}")
                    if fd == self.fds['cpu']:
                        self.cpu_event_count += 1
                        print(f"CPU PSI event {self.cpu_event_count}")
                    elif fd == self.fds['memory']:  # 新增
                        self.memory_event_count += 1
                        print(f"Memory PSI event {self.memory_event_count}")
                    elif fd == self.fds['io']:
                        self.io_event_count += 1
                        print(f"I/O PSI event {self.io_event_count}")
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Poll error: {e}")
                break

    def cleanup(self):
        """清理资源"""
        for fd in self.fds.values():
            os.close(fd)
        print("Resources cleaned up.")

if __name__ == "__main__":
    monitor = PSIEventMonitor()
    try:
        monitor.setup_polling()
        monitor.wait_for_events()
    finally:
        monitor.cleanup()
