import os

# Tests never reach Claude, even on a Mac with a key in the Keychain or one in the environment.
# The tests that cover Claude turn it back on with a fake key and a fake network.
os.environ["DESK_MUSIC_CLAUDE"] = "off"
