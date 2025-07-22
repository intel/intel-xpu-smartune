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

import threading
import time
import queue
from multiprocessing import Lock, JoinableQueue
from utils.logger import logger

def get_uuid():
    return uuid.uuid1().hex
class BaseBalancer:
    _instance = None
    _lock = Lock()  # 创建一个进程间共享的锁，用于保证单例模式的线程安全

    def __new__(cls, *args, **kwargs):
        with cls._lock:  # 使用锁来确保同一时刻只有一个进程能进入创建实例的逻辑
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.is_running = False
                cls._instance.thread = None
                cls._instance.task_queue = JoinableQueue(1000000)
                cls._instance.callbacks = {}
        return cls._instance

    def push_task(self, task, callback=None):
        #logger.info("BASE SERVICE receiver a task: %s", task)
        with self._lock:  # 添加锁来保护队列操作
            task_id = task['task_id']
            self.callbacks[task_id] = callback
            # logger.info("add this task to queue")
            self.task_queue.put(task)


    def get_status(self):
        return self.task_queue.empty()
    
    def start(self):
        """
        启动服务，包括启动服务线程来处理任务队列中的任务
        """
        if self.is_running:
            print("服务已经在运行，无需再次启动")
            return
        self.is_running = True
        self.thread = threading.Thread(target=self._run_loop)
        self.thread.start()

        self.monitor_thread = threading.Thread(target=self._monitor_loop)
        self.monitor_thread.start()
        print("服务已启动，线程已开始运行")


    def stop(self):
        """
        停止服务线程，设置运行标志为False，并等待线程结束，同时确保任务队列中的任务都已处理完成
        """

        print("服务开始停止.............")
        if not self.is_running:
            print("服务已经停止，无需再次操作")
            return
        self.is_running = False

        # 清空任务队列
        with self._lock: 
            while True:
                try:
                    if not self.is_running:
                        break
                    self.task_queue.get(block=False)
                    self.task_queue.task_done()
                except queue.Empty:
                    break
        self.task_queue.join()  # 等待任务队列中的所有任务都被标记完成
        self.thread.join()
        self.monitor_thread.join()
        print("服务已停止，线程已结束")


    def _run_loop(self):
        logger.info("service is wait for processing ")
        while self.is_running:
            #logger.info("waiting for a new task ")
            try:
                
                task = self.task_queue.get(block=True, timeout=5)
                task_id = task['task_id']
                result = self._process_task(task)

                with self._lock:  # 添加锁来保护回调函数字典操作
                    if task_id in self.callbacks:
                       callback = self.callbacks.pop(task_id)
                       if callback:
                           callback(result)

                    self.task_queue.task_done()  # 标记任务完成

            except queue.Empty:
                time.sleep(1)
        print("退出run_loop")


    def _monitor_loop(self):
        logger.info("monitor service is wait for processing ")

        self._preload_pending_tasks()
        while self.is_running:

            # 1. 检测新应用
            # self._detect_new_apps()

            # 2. 压力检测与自动平衡
            # self._auto_balance()

            time.sleep(5)

        print("退出_monitor_loop")


    def _process_task(self, task):
        """
        处理任务的抽象方法，子类需重写此方法来实现具体的任务处理逻辑，默认返回任务本身
        :param task: 要处理的任务
        :return: 任务处理结果，默认返回任务本身
        """
        return task
    def _preload_pending_tasks(self):
        """
        处理任务的抽象方法，子类需重写此方法来实现具体的任务处理逻辑，默认为空
        这个函数是可以resume之前进程结束前没有完成的任务
        """
        return

    def _has_task(self):
        """
        处理任务的抽象方法，子类需重写此方法来实现具体的任务处理逻辑，默认为空
        这个函数是去确认当前是否还有需要执行的任务
        """
        return

class TestService(BaseBalancer):
    def _process_task(self, task):
        """
        重写基类的处理任务方法，实现自己的业务逻辑，这里简单模拟任务处理耗时并返回结果
        :param task: 要处理的任务
        :return: 任务处理结果
        """
        time.sleep(2)  # 模拟任务处理耗时
        return {**task, "processed": True}  # 返回添加了处理标记的任务作为结果

if __name__ == "__main__":
    def task_callback(result):
        print(f"任务已完成，结果为: {result}")
    my_service = TestService()


    my_service1 = TestService()


    print(my_service is my_service1)  # 验证是否为同一个实例
    my_service.start()
    # 创建多个线程来推送任务
    threads = []
    for index in range(5):
        t = threading.Thread(target=lambda: my_service.push_task({"name": "task","id":index}, task_callback))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    input("press anyget")

    my_service.stop()

