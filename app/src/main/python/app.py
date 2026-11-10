"""Bootstrap Chaquopy: configura armazenamento gravável antes de importar o bot."""
import os

def start(data_dir):
    os.environ["BOT_DATA_DIR"] = str(data_dir)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = "8000"
    os.environ["BOT_LOCAL_ANDROID"] = "1"
    os.environ["BOT_CONVITE"] = "local-android"
    os.environ["BOT_MAX_USUARIOS"] = "1"
    import bot
    bot.android_start()
