import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import zipfile
from asyncio.exceptions import TimeoutError
from string import punctuation, whitespace
from time import time
from typing import List

import aiofiles
import aiohttp
from aiohttp import ClientConnectorError, ServerDisconnectedError
from bs4 import BeautifulSoup

import ccl_bplist

PROTOCOL = 'https://'
ILLEGAL_PATH_CHARS = punctuation.replace('.', '') + whitespace

DYNAMIC_PART_MOCK = 'telegram-crawler'

INPUT_FILENAME = os.environ.get('INPUT_FILENAME', 'tracked_links.txt')
INPUT_RES_FILENAME = os.environ.get('INPUT_FILENAME', 'tracked_res_links.txt')
OUTPUT_FOLDER = os.environ.get('OUTPUT_FOLDER', 'data/')
OUTPUT_MTPROTO_FOLDER = os.path.join(OUTPUT_FOLDER, os.environ.get('OUTPUT_MTPROTO_FOLDER', 'server/'))
OUTPUT_SITES_FOLDER = os.path.join(OUTPUT_FOLDER, os.environ.get('OUTPUT_SITES_FOLDER', 'web/'))
OUTPUT_CLIENTS_FOLDER = os.path.join(OUTPUT_FOLDER, os.environ.get('OUTPUT_CLIENTS_FOLDER', 'client/'))
OUTPUT_RESOURCES_FOLDER = os.path.join(OUTPUT_FOLDER, os.environ.get('OUTPUT_RESOURCES_FOLDER', 'web_res/'))

TRANSLATIONS_EN_CATEGORY_URL_REGEX = r'/en/[a-z_]+/[a-z_]+/$'

PAGE_GENERATION_TIME_REGEX = r'<!-- page generated in .+ -->'
PAGE_API_HASH_REGEX = r'\?hash=[a-z0-9]+'
PAGE_API_HASH_TEMPLATE = f'?hash={DYNAMIC_PART_MOCK}'
PASSPORT_SSID_REGEX = r'passport_ssid=[a-z0-9]+_[a-z0-9]+_[a-z0-9]+'
PASSPORT_SSID_TEMPLATE = f'passport_ssid={DYNAMIC_PART_MOCK}'
NONCE_REGEX = r'"nonce":"[a-z0-9]+_[a-z0-9]+_[a-z0-9]+'
NONCE_TEMPLATE = f'"nonce":"{DYNAMIC_PART_MOCK}'
PROXY_CONFIG_SUB_NET_REGEX = r'\d+\.\d+:8888;'
PROXY_CONFIG_SUB_NET_TEMPLATE = 'X.X:8888;'
TRANSLATE_SUGGESTION_REGEX = r'<div class="tr-value-suggestion">(.?)+</div>'
SPARKLE_SIG_REGEX = r';sig=(.*?);'
SPARKLE_SE_REGEX = r';se=(.*?);'
SPARKLE_SIG_TEMPLATE = f';sig={DYNAMIC_PART_MOCK};'
SPARKLE_SE_TEMPLATE = f';se={DYNAMIC_PART_MOCK};'

STEL_DEV_LAYER = 190

# unsecure but so simple
CONNECTOR = aiohttp.TCPConnector(ssl=False, force_close=True, limit=300)
TIMEOUT = aiohttp.ClientTimeout(total=10)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:99.0) Gecko/20100101 Firefox/99.0',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Cookie': f'stel_ln=en; stel_dev_layer={STEL_DEV_LAYER}',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0',
    'TE': 'trailers',
}

logging.basicConfig(format='%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def get_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def download_file(url: str, path: str, session: aiohttp.ClientSession):
    async with session.get(url) as response:
        if response.status != 200:
            return

        content = await response.read()

    async with aiofiles.open(path, mode='wb') as f:
        await f.write(content)


async def get_download_link_of_latest_appcenter_release(parameterized_url: str, session: aiohttp.ClientSession):
    api_base = 'https://install.appcenter.ms/api/v0.1'
    base_url = f'{api_base}/{parameterized_url}'

    async def make_req(url):
        async with session.get(url) as response:
            if response.status != 200:
                return

            return await response.json(encoding='UTF-8')

    json = await make_req(f'{base_url}/public_releases')
    if json and json[0]:
        latest_id = json[0]['id']
    else:
        return

    json = await make_req(f'{base_url}/releases/{latest_id}')
    if json:
        return json['download_url']

    return None


async def track_additional_files(
        files_to_track: List[str], input_dir_name: str, output_dir_name: str, encoding='utf-8', save_hash_only=False
):
    kwargs = {'mode': 'r', 'encoding': encoding}
    if save_hash_only:
        kwargs['mode'] = 'rb'
        del kwargs['encoding']

    for file in files_to_track:
        async with aiofiles.open(os.path.join(input_dir_name, file), **kwargs) as r_file:
            content = await r_file.read()

        if save_hash_only:
            content = get_hash(content)
        else:
            content = re.sub(r'id=".*"', 'id="tgcrawl"', content)
            content = re.sub(r'name="APKTOOL_DUMMY_.*" id', 'name="tgcrawl" id', content)

        filename = os.path.join(output_dir_name, file)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        async with aiofiles.open(filename, 'w', encoding='utf-8') as w_file:
            await w_file.write(content)


async def download_telegram_macos_beta_and_extract_resources(session: aiohttp.ClientSession):
    parameterized_url = 'apps/keepcoder/telegram-swift/distribution_groups/public'
    download_url = await get_download_link_of_latest_appcenter_release(parameterized_url, session)

    if not download_url:
        return

    crawled_data_folder = os.path.join(OUTPUT_CLIENTS_FOLDER, 'macos-beta')
    client_folder_name = 'macos'
    client_archive_name = 'macos.zip'

    assets_output_dir = 'macos_assets'
    assets_filename = 'Assets.car'
    assets_extractor = 'acextract'

    tool_download_url = 'https://github.com/MarshalX/acextract/releases/download/3.0/acextract'

    if 'darwin' not in platform.system().lower():
        await download_file(download_url, client_archive_name, session)
    else:
        await asyncio.gather(
            download_file(download_url, client_archive_name, session),
            download_file(tool_download_url, assets_extractor, session),
        )

    # synced
    with zipfile.ZipFile(client_archive_name, 'r') as f:
        f.extractall(client_folder_name)

    resources_path = 'Telegram.app/Contents/Resources'
    files_to_track = [
        f'{resources_path}/en.lproj/Localizable.strings',
    ]
    await track_additional_files(files_to_track, client_folder_name, crawled_data_folder, 'utf-16')

    _, _, hash_of_files_to_track = next(os.walk(f'{client_folder_name}/{resources_path}'))
    hash_of_files_to_track = [f'{resources_path}/{i}' for i in hash_of_files_to_track]
    await track_additional_files(hash_of_files_to_track, client_folder_name, crawled_data_folder, save_hash_only=True)

    def cleanup1():
        os.path.isdir(client_folder_name) and shutil.rmtree(client_folder_name)
        os.remove(client_archive_name)

    # .car crawler works only in macOS
    if 'darwin' not in platform.system().lower():
        cleanup1()
        return

    path_to_car = os.path.join(client_folder_name, resources_path, assets_filename)
    await (await asyncio.create_subprocess_exec('chmod', '+x', assets_extractor)).communicate()
    process = await asyncio.create_subprocess_exec(f'./{assets_extractor}', '-i', path_to_car, '-o', assets_output_dir)
    await process.communicate()

    def cleanup2():
        cleanup1()
        os.path.isdir(assets_output_dir) and shutil.rmtree(assets_output_dir)
        os.remove(assets_extractor)

    if process.returncode != 0:
        cleanup2()
        return

    _, _, hash_of_files_to_track = next(os.walk(assets_output_dir))
    await track_additional_files(
        hash_of_files_to_track,
        assets_output_dir,
        os.path.join(crawled_data_folder, assets_filename),
        save_hash_only=True
    )

    cleanup2()


async def download_telegram_ios_beta_and_extract_resources(session: aiohttp.ClientSession):
    # TODO fetch version automatically
    # ref: https://docs.github.com/en/rest/releases/releases#get-the-latest-release
    version = '8.7.2.23377'

    download_url = f'https://github.com/MarshalX/decrypted-telegram-ios/releases/download/{version}/Telegram-{version}.ipa'
    tool_download_url = 'https://github.com/MarshalX/acextract/releases/download/3.0/acextract'

    ipa_filename = f'Telegram-{version}.ipa'
    assets_extractor = 'acextract_ios'
    assets_filename = 'Assets.car'
    assets_output_dir = 'ios_assets'
    client_folder_name = 'ios'
    crawled_data_folder = os.path.join(OUTPUT_CLIENTS_FOLDER, 'ios-beta')

    if 'darwin' not in platform.system().lower():
        await download_file(download_url, ipa_filename, session)
    else:
        await asyncio.gather(
            download_file(download_url, ipa_filename, session),
            download_file(tool_download_url, assets_extractor, session),
        )

    # synced
    with zipfile.ZipFile(ipa_filename, 'r') as f:
        f.extractall(client_folder_name)

    resources_path = 'Payload/Telegram.app'

    files_to_convert = [
        f'{resources_path}/en.lproj/Localizable.strings',
        f'{resources_path}/en.lproj/InfoPlist.strings',
        f'{resources_path}/en.lproj/AppIntentVocabulary.plist',
    ]
    for filename in files_to_convert:
        path = os.path.join(client_folder_name, filename)

        # synced cuz ccl_bplist works with file objects and doesn't support asyncio
        with open(path, 'rb') as r_file:
            plist = ccl_bplist.load(r_file)

        async with aiofiles.open(path, 'w', encoding='utf-8') as w_file:
            await w_file.write(json.dumps(plist, indent=4))

    files_to_track = files_to_convert + [
        f'{resources_path}/_CodeSignature/CodeResources',
        f'{resources_path}/SC_Info/Manifest.plist',
    ]
    await track_additional_files(files_to_track, client_folder_name, crawled_data_folder)

    resources_folder = os.path.join(client_folder_name, resources_path)
    crawled_resources_folder = os.path.join(crawled_data_folder, resources_path)
    _, _, hash_of_files_to_track = next(os.walk(resources_folder))
    await track_additional_files(
        hash_of_files_to_track, resources_folder, crawled_resources_folder, save_hash_only=True
    )

    def cleanup1():
        os.path.isdir(client_folder_name) and shutil.rmtree(client_folder_name)
        os.remove(ipa_filename)

    # sry for copy-paste from macos def ;d

    # .car crawler works only in macOS
    if 'darwin' not in platform.system().lower():
        cleanup1()
        return

    path_to_car = os.path.join(resources_folder, assets_filename)
    await (await asyncio.create_subprocess_exec('chmod', '+x', assets_extractor)).communicate()
    process = await asyncio.create_subprocess_exec(f'./{assets_extractor}', '-i', path_to_car, '-o', assets_output_dir)
    await process.communicate()

    def cleanup2():
        cleanup1()
        os.path.isdir(assets_output_dir) and shutil.rmtree(assets_output_dir)
        os.remove(assets_extractor)

    if process.returncode != 0:
        cleanup2()
        return

    for dir_path, _, hash_of_files_to_track in os.walk(assets_output_dir):
        await track_additional_files(
            # sry for this shit ;d
            [os.path.join(dir_path, file).replace(f'{assets_output_dir}/', '') for file in hash_of_files_to_track],
            assets_output_dir,
            os.path.join(crawled_data_folder, assets_filename),
            save_hash_only=True
        )

    cleanup2()


async def download_telegram_android_beta_and_extract_resources(session: aiohttp.ClientSession):
    parameterized_url = 'apps/drklo-2kb-ghpo/telegram-beta-2/distribution_groups/all-users-of-telegram-beta-2'
    download_url = await get_download_link_of_latest_appcenter_release(parameterized_url, session)

    crawled_data_folder = os.path.join(OUTPUT_CLIENTS_FOLDER, 'android-beta')

    if not download_url:
        return

    await asyncio.gather(
        download_file('https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.6.1.jar', 'tool.apk', session),
        download_file(download_url, 'android.apk', session),
    )

    def cleanup():
        os.path.isdir('android') and shutil.rmtree('android')
        os.remove('tool.apk')
        os.remove('android.apk')

    process = await asyncio.create_subprocess_exec('java', '-jar', 'tool.apk', 'd', '-s', '-f', 'android.apk')
    await process.communicate()

    if process.returncode != 0:
        cleanup()
        return

    files_to_track = [
        'res/values/strings.xml',
        'res/values/public.xml'
    ]
    await track_additional_files(files_to_track, 'android', crawled_data_folder)

    cleanup()


async def collect_translations_paginated_content(url: str, session: aiohttp.ClientSession) -> str:
    headers = {'X-Requested-With': 'XMLHttpRequest'}
    content = dict()

    async def _get_page(offset: int):
        logger.info(f'Url: {url}, offset: {offset}')
        data = {'offset': offset, 'more': 1}

        try:
            new_offset = None
            async with session.post(
                    f'{PROTOCOL}{url}', data=data, headers=headers, allow_redirects=False, timeout=TIMEOUT
            ) as response:
                if response.status != 200:
                    logger.debug(f'Resend cuz {response.status}')
                    new_offset = offset
                else:
                    json = await response.json(encoding='UTF-8')
                    if 'more_html' in json and json['more_html']:
                        json['more_html'] = re.sub(TRANSLATE_SUGGESTION_REGEX, '', json['more_html'])

                        soup = BeautifulSoup(json['more_html'], 'html.parser')
                        tr_items = soup.find_all('div', {'class': 'tr-key-row-wrap'})
                        for tr_item in tr_items:
                            tr_key = tr_item.find_next('div', {'class': 'tr-value-key'}).text

                            tr_values = tr_item.find_all('span', {'class': 'value'})
                            tr_values_content = [tr_value.decode_contents() for tr_value in tr_values]

                            content[tr_key] = tr_values_content

                        new_offset = offset + 200

            new_offset and await _get_page(new_offset)
        except (TimeoutError, ClientConnectorError):
            logger.warning(f'Client or timeout error. Retrying {url}; offset {offset}')
            await _get_page(offset)

    await _get_page(0)

    content = dict(sorted(content.items()))
    return json.dumps(content, indent=4, ensure_ascii=False)


async def track_mtproto_configs():
    from pyrogram import Client

    kw = {
        'api_id': int(os.environ['TELEGRAM_API_ID']),
        'api_hash': os.environ['TELEGRAM_API_HASH'],
        'in_memory': True
    }

    app = Client('crawler', session_string=os.environ['TELEGRAM_SESSION'], **kw)
    app_test = Client('crawler_test', session_string=os.environ['TELEGRAM_SESSION_TEST'], test_mode=True, **kw)
    await app.start()
    await app_test.start()

    await _fetch_and_track_mtproto(app, '')
    await _fetch_and_track_mtproto(app_test, 'test')


async def _fetch_and_track_mtproto(app, output_dir):
    from pyrogram.raw import functions
    from pyrogram.raw.types import InputStickerSetAnimatedEmoji

    configs = {
        'GetConfig': await app.invoke(functions.help.GetConfig()),
        'GetCdnConfig': await app.invoke(functions.help.GetCdnConfig()),
        # 'GetInviteText': await app.invoke(functions.help.GetInviteText()),
        # 'GetSupport': await app.invoke(functions.help.GetSupport()),
        # 'GetSupportName': await app.invoke(functions.help.GetSupportName()),
        # 'GetPassportConfig': await app.invoke(functions.help.GetPassportConfig(hash=0)),
        'GetCountriesList': await app.invoke(functions.help.GetCountriesList(lang_code='en', hash=0)),
        'GetAppConfig': await app.invoke(functions.help.GetAppConfig()),
        # 'GetAppUpdate': await app.invoke(functions.help.GetAppUpdate(source='')),
        'AnimatedEmoji': await app.invoke(
            functions.messages.GetStickerSet(stickerset=InputStickerSetAnimatedEmoji(), hash=0)
        ),
    }

    keys_to_hide = {'access_hash', 'autologin_token', 'file_reference', 'file_reference_base64'}

    def rem_rec(config):
        if not isinstance(config, dict):
            return

        for key, value in config.items():
            if isinstance(value, dict):
                rem_rec(value)
            elif isinstance(value, list):
                for item in value:
                    rem_rec(item)
            elif key == 'key' and value in keys_to_hide:
                config['value']['value'] = 'crawler'
            elif key in keys_to_hide:
                config[key] = 'crawler'

    for config_name in {'GetAppConfig', 'AnimatedEmoji'}:
        configs[config_name] = json.loads(str(configs[config_name]))
        rem_rec(configs[config_name])
        configs[config_name] = json.dumps(configs[config_name], indent=4)

    configs['GetConfig'].date = 0
    configs['GetConfig'].expires = 0
    configs['GetConfig'].dc_options = []

    for file, content in configs.items():
        filename = os.path.join(OUTPUT_MTPROTO_FOLDER, output_dir, f'{file}.json')
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        async with aiofiles.open(filename, 'w', encoding='utf-8') as w_file:
            await w_file.write(str(content))

    await app.stop()


def is_hashable_only_content_type(content_type) -> bool:
    hashable_only_content_types = (
        'png',
        'jpeg',
        'x-icon',
        'gif',
        'mp4',
        'webm',
        'application/zip',
    )

    return any(
        hashable_only_content_type in content_type
        for hashable_only_content_type in hashable_only_content_types
    )


class RetryError(Exception):
    ...


async def crawl(url: str, session: aiohttp.ClientSession, output_dir: str = OUTPUT_SITES_FOLDER):
    while True:
        try:
            await _crawl(url, session, output_dir)
        except (RetryError, ServerDisconnectedError, TimeoutError, ClientConnectorError):
            logger.warning(f'Client or timeout error. Retrying {url}')
        else:
            break


async def _crawl(url: str, session: aiohttp.ClientSession, output_dir: str):
    logger.info(f'Process {url}')
    async with session.get(f'{PROTOCOL}{url}', allow_redirects=False, timeout=TIMEOUT, headers=HEADERS) as response:
        if 499 < response.status < 600:
            msg = f'Error 5XX. Retrying {url}'
            logger.warning(msg)
            raise RetryError(msg)

        if response.status not in {200, 304}:
            if response.status != 302:
                content = await response.text()
                logger.debug(f'Skip {url} because status code == {response.status}. Content: {content}')
            return

        # bypass external slashes and so on
        url_parts = [p for p in url.split('/') if p not in ILLEGAL_PATH_CHARS]

        is_hashable_only = is_hashable_only_content_type(response.content_type)
        # amazing dirt for media files like
        # telegram.org/file/811140591/1/q7zZHjgES6s/9d121a89ffb0015837
        # with response content type HTML instead of image. Shame on you
        # sometimes it returns correct type. noice load balancing
        is_sucking_file = '/file/' in url and 'text' in response.content_type

        # handle pure domains and html pages without ext in url
        ext = '.html' if '.' not in url_parts[-1] or len(url_parts) == 1 else ''

        # I don't add ext by content type for images and so on cuz TG servers sucks.
        # Some servers do not return correct content type. Some servers do...
        if is_hashable_only or is_sucking_file:
            ext = ''

        filename = os.path.join(output_dir, *url_parts) + ext
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        if is_sucking_file or is_hashable_only:
            content = await response.read()
            async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
                await f.write(get_hash(content))
            return

        content = await response.text(encoding='UTF-8')
        if re.search(TRANSLATIONS_EN_CATEGORY_URL_REGEX, url):
            content = await collect_translations_paginated_content(url, session)

        content = re.sub(PAGE_GENERATION_TIME_REGEX, '', content)
        content = re.sub(PAGE_API_HASH_REGEX, PAGE_API_HASH_TEMPLATE, content)
        content = re.sub(PASSPORT_SSID_REGEX, PASSPORT_SSID_TEMPLATE, content)
        content = re.sub(NONCE_REGEX, NONCE_TEMPLATE, content)
        content = re.sub(PROXY_CONFIG_SUB_NET_REGEX, PROXY_CONFIG_SUB_NET_TEMPLATE, content)
        content = re.sub(SPARKLE_SIG_REGEX, SPARKLE_SIG_TEMPLATE, content)
        content = re.sub(SPARKLE_SE_REGEX, SPARKLE_SE_TEMPLATE, content)

        async with aiofiles.open(filename, 'w', encoding='utf-8') as f:
            logger.info(f'Write to {filename}')
            await f.write(content)


async def crawl_web(session: aiohttp.ClientSession):
    with open(INPUT_FILENAME, 'r') as f:
        tracked_urls = {l.replace('\n', '') for l in f.readlines()}

    await asyncio.gather(*[crawl(url, session) for url in tracked_urls])


async def crawl_web_res(session: aiohttp.ClientSession):
    with open(INPUT_RES_FILENAME, 'r') as f:
        tracked_urls = {l.replace('\n', '') for l in f.readlines()}

    await asyncio.gather(*[crawl(url, session, OUTPUT_RESOURCES_FOLDER) for url in tracked_urls])


async def start(mode: str):
    async with aiohttp.ClientSession(connector=CONNECTOR) as session:
        mode == 'all' and await asyncio.gather(
            crawl_web(session),
            crawl_web_res(session),
            track_mtproto_configs(),
            download_telegram_android_beta_and_extract_resources(session),
            download_telegram_macos_beta_and_extract_resources(session),
            download_telegram_ios_beta_and_extract_resources(session),
        )
        mode == 'web' and await asyncio.gather(
            crawl_web(session),
        )
        mode == 'web_res' and await asyncio.gather(
            crawl_web_res(session),
        )
        mode == 'server' and await asyncio.gather(
            track_mtproto_configs(),
        )
        mode == 'client' and await asyncio.gather(
            download_telegram_android_beta_and_extract_resources(session),
            download_telegram_macos_beta_and_extract_resources(session),
            download_telegram_ios_beta_and_extract_resources(session),
        )


if __name__ == '__main__':
    run_mode = os.environ['MODE'] if 'MODE' in os.environ else 'all'
    start_time = time()
    logger.info('Start crawling content of tracked urls...')
    asyncio.get_event_loop().run_until_complete(start(run_mode))
    logger.info(f'Stop crawling content in mode {run_mode}. {time() - start_time} sec.')
