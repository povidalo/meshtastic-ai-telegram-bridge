import time
from datetime import datetime
import threading
import telebot
import telebot.types
import re
import uuid

import config

class Notifier(threading.Thread):
    def __init__(self, log):
        super(Notifier, self).__init__(name=config.TELEGRAM_NOTIFIER_NAME)
        self.__handle = config.TELEGRAM_BOT_HANDLE
        self.__token = config.TELEGRAM_BOT_TOKEN
        self.__queue: list[str] = []
        self.__queueLock = threading.Lock()
        self.__waitingQueueEvent = threading.Event()
        self.__bot = telebot.TeleBot(self.__token)
        self.__running = True
        self.__log = log
        self.start()

    def stop(self):
        if not self.__running:
            return
        with self.__queueLock:
            try:
                self.__waitingQueueEvent.set()
            finally:
                pass
        self.__running = False
        with self.__queueLock:
            try:
                self.__waitingQueueEvent.set()
            finally:
                pass
        dateStr = datetime.now().strftime('%Y-%m-%d %H_%M_%S')
        print(f'{dateStr} notifier stopped')

    def send(self, msg:str, chatId:int):
        with self.__queueLock:
            try:
                self.__queue.append((msg, chatId))
                self.__waitingQueueEvent.set()
            finally:
                pass

    def run(self):
        while (self.__running):
            msgs: list[(str, int)] = []
            with self.__queueLock:
                msgs = self.__queue
                self.__queue = []
                self.__waitingQueueEvent.clear()
            for item in msgs:
                msg = item[0].replace('-', '\-').replace('.', '\.').replace('=', '\=').replace('+', '\+').replace("!", "\!").replace(">", "\>").replace("<", "\<")
                pattern = re.compile(r'\[.*?\]\(http.*?\)')
                uuids = {}
                for m in re.finditer(pattern, msg):
                    if m.group(0) in uuids: continue
                    uuids[m.group(0)] = str(uuid.uuid4())
                for url in uuids:
                    msg = msg.replace(url, f'uuid_{uuids[url]}')
                msg = msg.replace("(", "\(").replace(")", "\)")
                for url in uuids:
                    msg = msg.replace(f'uuid_{uuids[url]}', url)
                chatId = item[1]
                try:
                    self.__bot.send_message(chatId, msg, parse_mode='MarkdownV2', link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=True))
                    self.__log.log('log', f'sent telegram message {msg}')
                except Exception as e:
                    self.__log.log('log', f'failed to send telegram message {msg} with error {e}')
                    pass
            if self.__running:
                self.__waitingQueueEvent.wait()
            
