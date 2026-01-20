#!/usr/bin/env python3

import os
import sys

# sys
sys.dont_write_bytecode = True
if sys.version_info[0] < 3:
    print('\033[91mERROR:\033[0m', 'you must be running python 3.0 or higher.')
    sys.exit()

# click
try:
    import click
except ImportError:
    print('\033[91mERROR:\033[0m', 'click is not installed.')
    sys.exit()

# plexapi
try:
    import plexapi.utils
    from plexapi.server import PlexServer
    from plexapi.exceptions import BadRequest
except ImportError:
    print('\033[91mERROR:\033[0m', 'plexapi is not installed.')
    sys.exit()

# defaults
NAME = 'plex-poster-exporter'
VERSION = 0.2

# plex
class Plex():
    def __init__(self, baseurl=None, token=None, library=None, overwrite=False, verbose=False, output_path=None):
        self.baseurl = baseurl
        self.token = token
        self.server = None
        self.libraries = []
        self.library = library
        self.overwrite = overwrite
        self.verbose = verbose
        self.output_path = output_path
        self.downloaded = 0
        self.skipped = 0

        self.getServer()
        self.getLibrary()

    def getServer(self):
        try:
            self.server = PlexServer(self.baseurl, self.token)
        except BadRequest as e:
            print('\033[91mERROR:\033[0m', 'failed to connect to Plex. Check your server URL and token.')
            sys.exit()

        if self.verbose:
            print('\033[94mSERVER:\033[0m', self.server.friendlyName)

    def getLibrary(self):
        self.libraries = [ _ for _ in self.server.library.sections() if _.type in {'movie', 'show'} ]
        if not self.libraries:
            print('\033[91mERROR:\033[0m', 'no available libraries.')
            sys.exit()
        if self.library == None or self.library not in [ _.title for _ in self.libraries ]:
            self.library = plexapi.utils.choose('Select Library', self.libraries, 'title')
        else:
            self.library = self.server.library.section(self.library)
        if self.verbose:
            print('\033[94mLIBRARY:\033[0m', self.library.title)

    def getAll(self):
        return self.library.all()

    def getPath(self, item, season=False):
        if self.library.type == 'movie':
            for media in item.media:
                for part in media.parts:
                    return os.path.dirname(part.file)
        elif self.library.type == 'show':
            for episode in item.episodes():
                for media in episode.media:
                    for part in media.parts:
                        if season:
                            # Returns the folder containing the episode (e.g., "Season 01")
                            return os.path.dirname(part.file)
                        # Returns the Show folder (parent of Season folder)
                        return os.path.dirname(os.path.dirname(part.file))

    def download(self, url=None, filename=None, path=None):
        # Handle cases where path might be absolute or relative
        if self.output_path == '/': 
            # If default, use the actual path from Plex
            abs_path = path
        else:
            # If user specified an override path (dry run logic), join them
            path = path.lstrip('/')
            abs_path = os.path.join(self.output_path, path)

        if not self.overwrite and os.path.isfile(os.path.join(abs_path, filename)):
            if self.verbose:
                print('\033[93mSKIPPED:\033[0m', os.path.join(abs_path, filename))
            self.skipped += 1
        else:
            if not os.path.exists(abs_path):
                try:
                    os.makedirs(abs_path)
                except:
                    pass
            
            if plexapi.utils.download(self.server._baseurl+url, self.token, filename=filename, savepath=abs_path):
                if self.verbose:
                    print('\033[92mDOWNLOADED:\033[0m', os.path.join(abs_path, filename))
                self.downloaded += 1
            else:
                print('\033[91mDOWNLOAD FAILED:\033[0m', os.path.join(abs_path, filename))
                # Don't exit on single fail, just print error
                pass 

# main
@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(prog_name=NAME, version=VERSION, message='%(prog)s v%(version)s')
@click.option('--baseurl', prompt='Plex Server URL', help='The base URL for the Plex server.', required=True)
@click.option('--token', prompt='Plex Token', help='The authentication token for Plex.', required=True)
@click.option('--library', help='The Plex library name.',)
@click.option('--assets', help='Which assets should be exported?', type=click.Choice(['all', 'posters', 'backgrounds', 'banners', 'themes']), default='all')
@click.option('--output-path', default='/', help='The output path for the downloaded assets. Leave default to save directly to media folders.')
@click.option('--overwrite', help='Overwrite existing assets?', is_flag=True)
@click.option('--verbose', help='Show extra information?', is_flag=True)
@click.pass_context
def main(ctx, baseurl: str, token: str, library: str, assets: str, overwrite: bool, verbose: bool, output_path: str):
    plex = Plex(baseurl, token, library, overwrite, verbose, output_path)

    if verbose:
        print('\033[94mASSETS:\033[0m', assets)
        print('\033[94mOVERWRITE:\033[0m', str(overwrite))
        print('\nGetting library items...')

    items = plex.getAll()

    for item in items:
        if verbose:
            print('\n\033[94mITEM:\033[0m', item.title)

        try:
            path = plex.getPath(item)
            if path is None:
                print(f'\033[93mWARNING:\033[0m Could not determine path for {item.title}, skipping.')
                continue

            # --- Movie / Show Level Assets ---
            if (assets == 'all' or assets == 'posters') and hasattr(item, 'thumb') and item.thumb:
                plex.download(item.thumb, 'poster.jpg', path)
            
            # Renamed 'background.jpg' to 'fanart.jpg' for better Jellyfin support
            if (assets == 'all' or assets == 'backgrounds') and hasattr(item, 'art') and item.art:
                plex.download(item.art, 'fanart.jpg', path)
            
            if (assets == 'all' or assets == 'banners') and hasattr(item, 'banner') and item.banner:
                plex.download(item.banner, 'banner.jpg', path)
            
            if (assets == 'all' or assets == 'themes') and hasattr(item, 'theme') and item.theme:
                plex.download(item.theme, 'theme.mp3', path)

            # --- TV Show Specific Logic ---
            if plex.library.type == 'show':
                for season in item.seasons():
                    # Get path to the season folder (e.g. /TV/Show/Season 1)
                    season_path = plex.getPath(season, True)
                    
                    if season_path:
                        # 1. Season Posters
                        # We save as 'folder.jpg' inside the season folder so Jellyfin sees it as the season cover
                        if (assets == 'all' or assets == 'posters') and hasattr(season, 'thumb') and season.thumb:
                            plex.download(season.thumb, 'folder.jpg', season_path)

                        # 2. Episode Thumbnails
                        # We iterate episodes to put thumbs next to files
                        for episode in season.episodes():
                            if (assets == 'all' or assets == 'posters') and hasattr(episode, 'thumb') and episode.thumb:
                                for media in episode.media:
                                    for part in media.parts:
                                        # Construct filename-thumb.jpg based on the video filename
                                        video_dir = os.path.dirname(part.file)
                                        video_filename = os.path.splitext(os.path.basename(part.file))[0]
                                        thumb_name = f"{video_filename}-thumb.jpg"
                                        
                                        # Download
                                        plex.download(episode.thumb, thumb_name, video_dir)
                                        # Break after first part found to avoid duplicates for multi-part episodes
                                        break 

        except Exception as e:
            print(f'\033[91mERROR Processing {item.title}:\033[0m {e}')

    if verbose:
        print('\n\033[94mTOTAL SKIPPED:\033[0m', str(plex.skipped))
        print('\033[94mTOTAL DOWNLOADED:\033[0m', str(plex.downloaded))

# run
if __name__ == '__main__':
    main(obj={})