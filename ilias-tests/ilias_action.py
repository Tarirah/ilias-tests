import asyncio
import datetime
import http.cookies
import json
import mimetypes
import random
import string
from pathlib import Path, PurePath
from random import randint
from typing import Any, Union, Callable, Optional

import aiohttp
from PFERD.auth import Authenticator
from PFERD.crawl import CrawlError
from PFERD.crawl.ilias.kit_ilias_html import IliasPage
from PFERD.crawl.ilias.kit_ilias_web_crawler import KitShibbolethLogin, KitIliasWebCrawler
from PFERD.logging import log
from PFERD.utils import soupify, fmt_path
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup

from .ilias_html import ExtendedIliasPage
from .spec import TestQuestion, PageDesignBlock, PageDesignBlockText, PageDesignBlockImage


class IliasInteractor:

    def __init__(
        self,
        authenticator: Authenticator,
        cookie_file: Path,
        http_timeout: int = 60,
    ) -> None:
        self._shibboleth_auth = KitShibbolethLogin(authenticator, None)
        self._cookie_jar = aiohttp.CookieJar()
        self._cookie_file = cookie_file
        self._authentication_id = 0
        self._authentication_lock = asyncio.Lock()
        self._request_count = 0

        self._load_cookies()

        self.session = aiohttp.ClientSession(
            headers={"User-Agent": f"Foobar"},
            cookie_jar=self._cookie_jar,
            # connector=aiohttp.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where())),
            connector=aiohttp.TCPConnector(verify_ssl=False),
            timeout=ClientTimeout(
                # 30 minutes. No download in the history of downloads was longer than 30 minutes.
                # This is enough to transfer a 600 MB file over a 3 Mib/s connection.
                # Allowing an arbitrary value could be annoying for overnight batch jobs
                total=15 * 60,
                connect=http_timeout,
                sock_connect=http_timeout,
                sock_read=http_timeout,
            )
        )

    def _load_cookies(self) -> None:
        jar: Any = http.cookies.SimpleCookie()
        with open(self._cookie_file, encoding="utf-8") as f:
            for i, line in enumerate(f):
                # Names of headers are case-insensitive
                if line[:11].lower() == "set-cookie:":
                    jar.load(line[11:])
                else:
                    log.explain(f"Line {i} doesn't start with 'Set-Cookie:', ignoring it")
        self._cookie_jar.update_cookies(jar)

    def _save_cookies(self) -> None:
        jar: Any = http.cookies.SimpleCookie()
        for morsel in self._cookie_jar:
            jar[morsel.key] = morsel
        with open(self._cookie_file, "w", encoding="utf-8") as f:
            f.write(jar.output(sep="\n"))
            f.write("\n")  # A trailing newline is just common courtesy

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._save_cookies()
        return await self.session.__aexit__(exc_type, exc_val, exc_tb)

    async def navigate_to_folder(self, base_url: str, path: PurePath) -> ExtendedIliasPage:
        page = await self._get_extended_page(base_url)
        for part in path.parts:
            found_child = False
            for child in page.get_child_elements():
                if child.name == part:
                    page = await self._get_extended_page(child.url)
                    found_child = True
            if not found_child:
                raise CrawlError(f"Could not find folder {part!r} in {fmt_path(path)}")
        return page

    async def create_test(self, folder_url: str, title: str, description: str) -> ExtendedIliasPage:
        folder = await self._get_extended_page(folder_url)
        create_url = folder.get_test_create_url()
        if not create_url:
            raise CrawlError("Could not find test create URL")
        create_page = await self._get_extended_page(create_url)

        submit_url, submit_value = create_page.get_test_create_submit_url()

        return await self._post_authenticated(
            submit_url,
            data={
                "title": title,
                "desc": description,
                "save": submit_value
            },
            request_succeeded=_auth_redirected_to_test_page
        )

    async def select_tab(self, page: ExtendedIliasPage, name: str):
        return await self._get_extended_page(page.get_test_tabs()[name])

    async def select_page(self, url: str):
        return await self._get_extended_page(url)

    async def configure_test(
        self,
        settings_page: ExtendedIliasPage,
        title: str,
        description: str,
        intro_text: str,
        starting_time: Optional[datetime.datetime],
        ending_time: Optional[datetime.datetime],
        numer_of_tries: int
    ):
        base_params = {
            "cmd[saveForm]": "Speichern",
            "title": title,
            "description": description,
            "use_pool": "0",  # use questions from pool
            "question_set_type": "FIXED_QUEST_SET",  # everybody gets the same questions
            "anonymity": "0",
        }
        activation_params = {
            "online": "0",
            # "activation_type": "1",  # time limited
            # "access_period[start]": _format_time(datetime.datetime.now()),  # start of it
            # "access_period[end]": _format_time(datetime.datetime.now()),  # end of it
            # "activation_visibility": "1"  # always visible, but not take-able
        }
        intro_params = {
            "showinfo": "1",  # show users the info tab
            "intro_enabled": "1",  # show text before the test
            "introduction": intro_text,  # the text
        }
        access_params = {
            "starting_time": _format_time(starting_time),
            "ending_time": _format_time(ending_time)
        }
        run_test_params = {
            "limitPasses": "1",
            "nr_of_tries": str(numer_of_tries),
        }
        run_question_params = {
            "title_output": "0",  # show title and max points
            "answer_fixation_handling": "none",  # allow changing answers
        }
        run_user_params = {
            "chb_use_previous_answers": "1",  # show answers from previous run
            "postpone": "0"  # do not move unanswered questions to the end
        }
        other_params = {
            "autosave_ival": "30",
            "instant_feedback_trigger": "0"
        }

        data = {
            **base_params, **activation_params, **intro_params, **access_params,
            **run_test_params, **run_question_params, **run_user_params, **other_params
        }
        url, extra_data = settings_page.get_test_settings_change_data()

        def build_form_data():
            form_data = aiohttp.FormData()
            for key, val in data.items():
                form_data.add_field(key, val)

            for key, val in extra_data.items():
                if key not in data:
                    form_data.add_field(key, val)

            form_data.add_field(
                name="tile_image",
                value=b"",
                content_type="application/octet-stream",
                filename="",
            )
            return form_data

        return await self._post_authenticated(
            url=url,
            data=build_form_data
        )

    async def add_question(
        self,
        question_page: ExtendedIliasPage,
        question: TestQuestion
    ):
        url = question_page.get_test_add_question_url()
        page = await self._get_extended_page(url)

        edit_page = await self._post_authenticated(
            page.get_test_question_create_url(),
            data={
                "cmd[executeCreateQuestion]": "Erstellen",
                "qtype": str(question.question_type.value),
                "add_quest_cont_edit_mode": "default",  # TinyMCE
                "usage": "1",  # no question pool
                "position": "0"  # just put it at the beginning
            },
            soup_succeeded=lambda pg: pg.is_test_question_edit_page()
        )

        post_data = {
            **question.get_options(),
            "cmd[saveReturn]": "Speichern und zurückkehren"
        }

        url, defaults = edit_page.get_test_question_finalize_data()
        for key, val in defaults.items():
            if key not in post_data:
                post_data[key] = val

        def build_form_data():
            form_data = aiohttp.FormData()
            for post_key, post_val in post_data.items():
                if isinstance(post_val, Path):
                    form_data.add_field(name=post_key, value=b"", content_type="application/octet-stream", filename="")
                else:
                    form_data.add_field(post_key, post_val)

            return form_data

        question_page = await self._post_authenticated(url, data=build_form_data)
        design_page = await self.select_page(question_page.get_test_question_design_page_url())
        await self.design_page_add_blocks(design_page, question.page_design)
        return question_page

    async def reorder_questions(self, questions_tab: ExtendedIliasPage, title_order: list[str]):
        ids = questions_tab.get_test_question_ids()
        log.explain_topic("Question ids")
        log.explain(str(ids))
        question_to_position = {}
        for index, title in enumerate(title_order):
            question_to_position[ids[title]] = index

        url, data = questions_tab.get_test_question_save_order_data(question_to_position)
        await self._post_authenticated(
            url=url,
            data=data
        )

    async def select_edit_question(self, question_url: str):
        page = await self.select_page(question_url)
        if page.is_test_question_custom_page():
            print("Hello", question_url)
        return await self.select_page(page.get_test_question_edit_url())

    async def design_page_add_blocks(self, edit_page: ExtendedIliasPage, blocks: list[PageDesignBlock]):
        current_id = ""
        for block in blocks:
            match block:
                case PageDesignBlockImage(image=image):
                    current_id = await self.design_page_add_image_block(edit_page, path=image, after_id=current_id)
                case PageDesignBlockText(text_html=text):
                    current_id = await self.design_page_add_text_block(edit_page, text_html=text, after_id=current_id)
                case _:
                    raise CrawlError("Unknown page design block")

    async def design_page_add_text_block(self, edit_page: ExtendedIliasPage, text_html: str, after_id: str) -> str:
        post_url = edit_page.get_test_question_design_post_url()
        new_id = "".join([str(randint(0, 9)) for _ in range(20)])
        resp = await self._post_authenticated_json(
            url=post_url,
            data={
                "action_id": 11,
                "component": "Paragraph",
                "action": "insert",
                "data": {
                    "after_pcid": after_id,
                    "pcid": new_id,
                    "content": text_html,
                    "characteristic": "Standard",
                    "fromPlaceholder": False
                }
            },
        )
        if resp["error"]:
            raise CrawlError(f"Adding text block failed with: {resp['error']!r}")
        return new_id

    async def design_page_add_image_block(self, edit_page: ExtendedIliasPage, path: Path, after_id: str) -> str:
        post_url = edit_page.get_test_question_design_post_url()
        new_id = "".join([str(randint(0, 9)) for _ in range(20)])

        post_data = {
            "standard_file": path,
            "standard_type": "File",
            "standard_size": "original",
            "full_type": "None",
            "action_id": "10",
            "component": "MediaObject",
            "action": "insert",
            "after_pcid": after_id,
            "pcid": new_id,
            "ilfilehash": ''.join(random.choice(string.ascii_lowercase + "0123456789") for _ in range(32))
        }

        def build_form_data():
            form_data = aiohttp.FormData()
            for post_key, post_val in post_data.items():
                if isinstance(post_val, Path):
                    form_data.add_field(
                        name=post_key,
                        value=open(post_val, "rb"),
                        content_type=mimetypes.guess_type(post_val)[0],
                        filename=str(post_val.name)
                    )
                else:
                    form_data.add_field(post_key, post_val)

            return form_data

        await self._post_authenticated(
            url=post_url,
            data=build_form_data,
            soup_succeeded=lambda x: print(x) or True
        )
        return new_id

    async def download_file(self, url: str, output_folder: Path, prefix: str):
        auth_id = await self._current_auth_id()
        if not output_folder.exists():
            output_folder.mkdir(parents=True, exist_ok=True)

        async def do_request():
            async with self.session.get(url) as response:
                if 200 <= response.status < 300:
                    filename = prefix + response.headers.get("content-description", "")
                    content = await response.read()
                    out_path = output_folder / filename
                    with open(out_path, "wb") as file:
                        file.write(content)
                    return out_path
            return None

        if output_file := await do_request():
            return output_file

        # We weren't authenticated, so try to do that
        await self.authenticate(auth_id)

        # Retry once after authenticating. If this fails, we will die.
        if output_file := await do_request():
            return output_file
        raise CrawlError(f"download_file failed even after authenticating on {url!r}")

    async def _get_extended_page(self, url: str) -> ExtendedIliasPage:
        return ExtendedIliasPage(await self._get_soup(url), url)

    async def _get_soup(self, url: str, root_page_allowed: bool = False) -> BeautifulSoup:
        auth_id = await self._current_auth_id()

        async def do_request():
            async with self.session.get(url) as request:
                soup = soupify(await request.read())
                if IliasPage.is_logged_in(soup):
                    # noinspection PyProtectedMember
                    return KitIliasWebCrawler._verify_page(soup, url, root_page_allowed)
            return None

        if page := await do_request():
            return page

        # We weren't authenticated, so try to do that
        await self.authenticate(auth_id)

        # Retry once after authenticating. If this fails, we will die.
        if page := await do_request():
            return page
        raise CrawlError(f"get_page failed even after authenticating on {url!r}")

    async def _post_authenticated(
        self,
        url: str,
        data: Union[dict[str, Union[str, list[str]]], Callable[[], aiohttp.FormData]],
        request_succeeded: Callable[[aiohttp.ClientResponse], bool] = lambda resp: 200 <= resp.status < 300,
        soup_succeeded: Callable[[ExtendedIliasPage], bool] = ExtendedIliasPage.page_has_success_alert,
    ) -> ExtendedIliasPage:
        auth_id = await self._current_auth_id()

        def build_form_data():
            if isinstance(data, dict):
                form_data = aiohttp.FormData()
                for key, val in data.items():
                    form_data.add_field(key, val)
                return form_data
            else:
                return data()

        async def do_request():
            async with self.session.post(url, data=build_form_data(), allow_redirects=True) as response:
                log.explain_topic("Checking response")
                if request_succeeded(response):
                    log.explain("Checking soup")
                    my_page = ExtendedIliasPage(soupify(await response.read()), str(response.url))
                    if soup_succeeded(my_page):
                        return my_page

        if page := await do_request():
            return page

        # We weren't authenticated, so try to do that
        await self.authenticate(auth_id)

        # Retry once after authenticating. If this fails, we will die.
        if page := await do_request():
            return page

        raise CrawlError("post_authenticated failed even after authenticating")

    async def _post_authenticated_json(self, url: str, data: Any) -> Any:
        auth_id = await self._current_auth_id()

        async def do_request():
            async with self.session.post(
                url, data=json.dumps(data), allow_redirects=True, headers={"Content-Type": "application/json"}
            ) as response:
                log.explain_topic("Checking response")
                if 200 <= response.status < 300:
                    return json.loads(await response.read())

        if page := await do_request():
            return page

        # We weren't authenticated, so try to do that
        await self.authenticate(auth_id)

        # Retry once after authenticating. If this fails, we will die.
        if page := await do_request():
            return page

        raise CrawlError("post_authenticated_json failed even after authenticating")

    async def _current_auth_id(self) -> int:
        """
        Returns the id for the current authentication, i.e. an identifier for the last
        successful call to [authenticate].

        This method must be called before any request that might authenticate is made, so the
        HttpCrawler can properly track when [authenticate] can return early and when actual
        authentication is necessary.
        """
        # We acquire the lock here to ensure we wait for any concurrent authenticate to finish.
        # This should reduce the amount of requests we make: If an authentication is in progress
        # all future requests wait for authentication to complete.
        async with self._authentication_lock:
            self._request_count += 1
            return self._authentication_id

    async def authenticate(self, caller_auth_id: int) -> None:
        """
        Starts the authentication process. The main work is offloaded to _authenticate, which
        you should overwrite in a subclass if needed. This method should *NOT* be overwritten.

        The [caller_auth_id] should be the result of a [_current_auth_id] call made *before*
        the request was made. This ensures that authentication is not performed needlessly.
        """
        async with self._authentication_lock:
            log.explain_topic("Authenticating")
            # Another thread successfully called authenticate in-between
            # We do not want to perform auth again, so we return here. We can
            # assume the other thread succeeded as authenticate will throw an error
            # if it failed and aborts the crawl process.
            if caller_auth_id != self._authentication_id:
                log.explain(
                    "Authentication skipped due to auth id mismatch."
                    "A previous authentication beat us to the race."
                )
                return
            log.explain("Calling crawler-specific authenticate")
            await self._authenticate()
            self._authentication_id += 1
            # Saving the cookies after the first auth ensures we won't need to re-authenticate
            # on the next run, should this one be aborted or crash
            self._save_cookies()

    async def _authenticate(self) -> None:
        await self._shibboleth_auth.login(self.session)


def _auth_redirected_to_test_page(response: aiohttp.ClientResponse):
    return "cmdClass=ilobjtestsettingsgeneralgui" in response.url.query_string


def _format_time(time: Optional[datetime.datetime]) -> str:
    if not time:
        return ""
    return time.strftime("%d.%m.%Y %H:%M")
