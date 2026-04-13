import threading
import time
from datetime import datetime
import os
from utils.logger import LoggerWorker as worker
from utils.logger.models import WorkerData

class Logger(threading.Thread):
    def __init__(self, poolSize=5, baseDir='', queueDumpSize:int=0):
        super(Logger, self).__init__()
        if not os.path.isdir(baseDir):
            os.makedirs(baseDir)
            #raise Exception("Directory does not exist {}".format(baseDir))
        self.poolSize = poolSize
        self.queueDumpSize = queueDumpSize
        self.baseDir = baseDir
        self.running = True
        self.queue: list[WorkerData] = []
        self.queueLock = threading.Lock()
        self.processingKeys = []
        self.messageCounter = 0
        self.waitingQueueEvent = threading.Event()
        self.start()

    def log(self, key:str, data:str, silent=False):
        with self.queueLock:
            try:
                self.queue.append(WorkerData(key, round(time.time() * 1000), data, silent))
                if not silent:
                    dateStr = datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H_%M_%S')
                    print(f'{key}:{dateStr}: {data}')
                if len(self.queue) > self.queueDumpSize:
                    self.waitingQueueEvent.set()
            finally:
                pass

    def stop(self):
        if not self.running:
            return
        with self.queueLock:
            try:
                self.waitingQueueEvent.set()
            finally:
                pass
        self.running = False
        with self.queueLock:
            try:
                self.waitingQueueEvent.set()
            finally:
                pass
        dateStr = datetime.now().strftime('%Y-%m-%d %H_%M_%S')
        print(f'{dateStr} logger stopped')

    def workerFinished(self, key):
        with self.queueLock:
            try:
                self.messageCounter += 1
                if self.messageCounter % 2000 == 0:
                    print(f'{self.messageCounter} messages has been written')
                self.processingKeys.remove(key)
                self.waitingQueueEvent.set()
            finally:
                pass

    def run(self):
        while (self.running):
            nextItems: list[WorkerData] = []
            key = None
            with self.queueLock:
                try:
                    if len(self.processingKeys) < self.poolSize and len(self.queue) > 0:
                        updatedQueue: list[WorkerData] = []
                        for i in range(len(self.queue)):
                            if self.queue[i].key not in self.processingKeys:
                                if key is None:
                                    key = self.queue[i].key
                                    self.processingKeys.append(key)
                            if key is not None and self.queue[i].key == key:
                                nextItems.append(self.queue[i])
                            else:
                                updatedQueue.append(self.queue[i])
                        self.queue = updatedQueue
                finally:
                    pass
            if len(nextItems) <= 0:
                if self.running:
                    self.waitingQueueEvent.wait()
                self.waitingQueueEvent.clear()
                continue

            worker.LoggerWorker(logger=self, key=key, data=nextItems, file=f'{key}.log', baseDir=self.baseDir)