import threading
import os
from datetime import datetime
from utils.logger.models import WorkerData

class LoggerWorker(threading.Thread):
    def __init__(self, logger, key:str, data:list[WorkerData], file, baseDir):
        super(LoggerWorker, self).__init__()
        self.logger = logger
        self.key:str = key
        self.data:list[WorkerData] = data
        self.file = file
        self.baseDir = baseDir
        self.start()

    def run(self):
        fileName = os.path.join(self.baseDir, self.file)

        with open(fileName, "a") as logFile:
            for data in self.data:
                dateStr = datetime.fromtimestamp(float(data.time)/1000.0).strftime('%Y-%m-%d %H_%M_%S')
                dataStr = f'{data.time}:{dateStr}:{self.key}:{data.data}'
                print(dataStr, file=logFile)
        os.chmod(fileName, 0o666)

        self.logger.workerFinished(self.key)
