#!/usr/bin/env python3

import os
import sys
import datetime

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
    from plexapi.exceptions import BadRequest, NotFound
except ImportError:
    print('\033[91mERROR:\033[0m', 'plexapi is not installed.')
    sys.exit()

# tqdm (optional)
try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# defaults
NAME = 'plex-poster-exporter'
VERSION = 0.5

# plex
class Plex():
    def __init__(self, baseurl=None, token=None, library=None, force=False, verbose=False, output_path=None, dry_run=False):
        self.baseurl = baseurl
        self.token = token
        self.server = None
        self.libraries = []
        self.library = library
        self.force = force
        self.verbose = verbose
        self.output_path = output_path
        self.dry_run = dry_run
        self.downloaded = 0
        self.skipped = 0

        self.getServer()
        self.getLibrary()

    def getServer(self):
        try:
            self.server = PlexServer(self.baseurl, self.token)
        except BadRequest:
            print('\033[91mERROR:\033[0m', 'failed to connect to Plex. Check your server URL and token.')
            sys.exit()

        if self.verbose:
            print('\033[94mSERVER:\033[0m', self.server.friendlyName)

    def getLibrary(self):
        self.libraries = [_ for _ in self.server.library.sections() if _.type in {'movie', 'show'}]
        if not self.libraries:
            print('\033[91mERROR:\033[0m', 'no available libraries.')
            sys.exit()
        if self.library is None or self.library not in [_.title for _ in self.libraries]:
            self.library = plexapi.utils.choose('Select Library', self.libraries, 'title')
        else:
            self.library = self.server.library.section(self.library)
        if self.verbose:
            print('\033[94mLIBRARY:\033[0m', self.library.title)

    def getAll(self):
        return self.library.all()

    def getPath(self, item):
        """
        Use item.locations which Plex provides directly and accurately.
        For movies, locations contains file paths — return the parent directory.
        For shows and seasons, locations contains directory paths — return as-is.
        """
        if hasattr(item, 'locations') and item.locations:
            loc = item.locations[0]
            if self.library.type == 'movie':
                return os.path.dirname(loc)
            else:
                # show or season: locations is already the directory
                return loc
        return None

    def download(self, url=None, filename=None, path=None, source_updated_at=None):
        if not url or not path:
            return

        # Guard against library root paths (less than 2 path segments deep)
        stripped = path.strip('/')
        if len(stripped.split('/')) < 2:
            print(f'\033[91mSKIPPED (Unsafe Path):\033[0m {path} looks like a library root — refusing to write here.')
            return

        if self.output_path == '/':
            abs_path = path
        else:
            path = path.lstrip('/')
            abs_path = os.path.join(self.output_path, path)

        final_file_path = os.path.join(abs_path, filename)
        should_download = True

        # SMART SYNC LOGIC
        if os.path.isfile(final_file_path):
            if self.force:
                should_download = True
            elif source_updated_at:
                local_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(final_file_path))
                # 1 minute buffer for clock drift
                if source_updated_at > (local_mtime + datetime.timedelta(seconds=60)):
                    should_download = True
                    if self.verbose:
                        print(f'  [UPDATE DETECTED] Plex: {source_updated_at} > Local: {local_mtime}')
                else:
                    should_download = False
            else:
                should_download = False

        if not should_download:
            if self.verbose:
                print('\033[93mSKIPPED (Current):\033[0m', final_file_path)
            self.skipped += 1
            return

        if self.dry_run:
            print(f'\033[96mDRY RUN:\033[0m Would download → {final_file_path}')
            self.downloaded += 1
            return

        if not os.path.exists(abs_path):
            try:
                os.makedirs(abs_path)
            except Exception:
                pass

        try:
            full_url = self.server._baseurl + url
            if plexapi.utils.download(full_url, self.token, filename=filename, savepath=abs_path):
                if self.verbose:
                    print('\033[92mDOWNLOADED:\033[0m', final_file_path)
                self.downloaded += 1
            else:
                print('\033[91mDOWNLOAD FAILED (Generic):\033[0m', final_file_path)
        except NotFound:
            print(f'\033[91mNOT FOUND (404):\033[0m Plex cannot find the asset file for {filename}.')
        except Exception as e:
            print(f'\033[91mERROR:\033[0m {e}')


# main
@click.command(context_settings=dict(help_option_names=['-h', '--help']))
@click.version_option(prog_name=NAME, version=VERSION, message='%(prog)s v%(version)s')
@click.option('--baseurl', prompt='Plex Server URL', help='The base URL for the Plex server.', required=True)
@click.option('--token', prompt='Plex Token', help='The authentication token for Plex.', required=True)
@click.option('--library', help='The Plex library name.')
@click.option('--assets', help='Which assets should be exported?', type=click.Choice(['all', 'posters', 'backgrounds', 'banners', 'themes']), default='all')
@click.option('--output-path', default='/', help='The output path for the downloaded assets.')
@click.option('--force', help='Force overwrite of all assets regardless of timestamp?', is_flag=True)
@click.option('--dry-run', help='Log what would be downloaded without writing any files.', is_flag=True)
@click.option('--verbose', help='Show extra information?', is_flag=True)
@click.pass_context
def main(ctx, baseurl: str, token: str, library: str, assets: str, force: bool, verbose: bool, output_path: str, dry_run: bool):
    plex = Plex(baseurl, token, library, force, verbose, output_path, dry_run)

    if dry_run:
        print('\033[96mDRY RUN MODE — no files will be written.\033[0m')

    if verbose:
        print('\033[94mASSETS:\033[0m', assets)
        print('\033[94mFORCE OVERWRITE:\033[0m', str(force))
        print('\nGetting library items...')

    items = plex.getAll()

    use_progress = TQDM_AVAILABLE and not verbose
    iterator = tqdm(items, desc='Exporting', unit='item') if use_progress else items

    for item in iterator:
        # Reload to get freshest URLs and timestamps
        try:
            item.reload()
        except Exception:
            continue

        if verbose:
            print('\n\033[94mITEM:\033[0m', item.title)

        try:
            path = plex.getPath(item)
            if path is None:
                if verbose:
                    print(f'  [SKIP] No path found for {item.title}')
                continue

            # Use None as fallback so timestamp logic doesn't fire on stale data
            item_updated = getattr(item, 'updatedAt', None)

            # MOVIE / SHOW LEVEL ASSETS
            if (assets == 'all' or assets == 'posters') and getattr(item, 'thumb', None):
                plex.download(item.thumb, 'poster.jpg', path, item_updated)

            if (assets == 'all' or assets == 'backgrounds') and getattr(item, 'art', None):
                plex.download(item.art, 'fanart.jpg', path, item_updated)

            if (assets == 'all' or assets == 'banners') and getattr(item, 'banner', None):
                plex.download(item.banner, 'banner.jpg', path, item_updated)

            if (assets == 'all' or assets == 'themes') and getattr(item, 'theme', None):
                plex.download(item.theme, 'theme.mp3', path, item_updated)

            # TV SPECIFIC
            if plex.library.type == 'show':
                for season in item.seasons():
                    # Use item.locations-based path for the season
                    season_path = plex.getPath(season)
                    # Fallback: None so timestamp logic doesn't fire incorrectly
                    season_updated = getattr(season, 'updatedAt', None)

                    if not season_path:
                        continue

                    if (assets == 'all' or assets == 'posters') and getattr(season, 'thumb', None):
                        plex.download(season.thumb, 'folder.jpg', season_path, season_updated)

                    if (assets == 'all' or assets == 'backgrounds') and getattr(season, 'art', None):
                        plex.download(season.art, 'season-fanart.jpg', season_path, season_updated)

                    if (assets == 'all' or assets == 'banners') and getattr(season, 'banner', None):
                        plex.download(season.banner, 'season-banner.jpg', season_path, season_updated)

                    for episode in season.episodes():
                        episode_updated = getattr(episode, 'updatedAt', None)

                        if (assets == 'all' or assets == 'posters') and getattr(episode, 'thumb', None):
                            # Find the episode file path — break cleanly out of both loops
                            ep_path = None
                            ep_filename = None
                            for media in episode.media:
                                for part in media.parts:
                                    ep_path = os.path.dirname(part.file)
                                    ep_filename = os.path.splitext(os.path.basename(part.file))[0] + '-thumb.jpg'
                                    break
                                if ep_path:
                                    break
                            if ep_path and ep_filename:
                                plex.download(episode.thumb, ep_filename, ep_path, episode_updated)

        except Exception as e:
            print(f'\033[91mERROR Processing {item.title}:\033[0m {e}')

    print()
    if dry_run:
        print('\033[96mDRY RUN COMPLETE\033[0m')
        print('\033[94mWOULD DOWNLOAD:\033[0m', str(plex.downloaded))
    else:
        print('\033[94mTOTAL SKIPPED:\033[0m', str(plex.skipped))
        print('\033[94mTOTAL DOWNLOADED:\033[0m', str(plex.downloaded))


# run
if __name__ == '__main__':
    main(obj={})
