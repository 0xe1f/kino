from db import db
from dao.user import UserDAO
from dao.video import VideoDAO
from dao.playlist import PlaylistDAO, PlaylistItemDAO
from dao.reaction import ReactionDAO
from dao.playback import PlaybackDAO

users = UserDAO(db)
videos = VideoDAO(db)
playlists = PlaylistDAO(db)
playlist_items = PlaylistItemDAO(db)
reactions = ReactionDAO(db)
playback = PlaybackDAO(db)
