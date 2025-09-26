import os
import select

class PSIMonitor:
    RESOURCES = ['cpu', 'memory', 'io']

    def __init__(self):
        self.base_path = "/proc/pressure"
        self.fds = {}  # 文件描述符缓存

    def _get_fd(self, resource):
        """获取或创建文件描述符"""
        if resource not in self.fds:
            path = os.path.join(self.base_path, resource)
            fd = os.open(path, os.O_RDONLY)
            self.fds[resource] = fd
        return self.fds[resource]

    def read_instant_pressure(self, resource, metric='some'):
        """读取瞬时PSI值（通过poll机制）"""
        fd = self._get_fd(resource)
        try:
            # 重置文件指针并触发新事件
            os.lseek(fd, 0, os.SEEK_SET)
            # 使用poll等待数据更新（超时0表示立即返回）
            poll = select.poll()
            poll.register(fd, select.POLLPRI)
            poll.poll(0)  # 非阻塞模式
            # 读取最新数据
            data = os.read(fd, 4096).decode()
            for line in data.splitlines():
                if line.startswith(metric):
                    return float(line.split()[1].split('=')[1])
        except Exception:
            return 0.0

    def get_current_pressure(self):
        """获取所有资源的瞬时压力"""
        return {
            res: self.read_instant_pressure(res)
            for res in self.RESOURCES
        }

    def __del__(self):
        """清理文件描述符"""
        for fd in self.fds.values():
            os.close(fd)
