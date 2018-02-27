'''
builtin post_fetch event handler
special seed handling -- don't count redirs in depth, and add www.seed if naked seed fails (or reverse)
do this by elaborating the work unit to have an arbitrary callback

parse links and embeds, using an algorithm chosen in conf -- cocrawler.cocrawler

parent subsequently calls add_url on them -- cocrawler.cocrawler
'''

import logging
import cgi
from functools import partial
import json

from . import urls
from . import parse
from . import stats
from . import config
from . import seeds
from . import facet
from . import geoip

LOGGER = logging.getLogger(__name__)


# aiohttp.ClientReponse lacks this method, so...
def is_redirect(response):
    return 'Location' in response.headers and response.status in (301, 302, 303, 307, 308)


def minimal_facet_me(resp_headers, url, host_geoip, seed_host, kind, t, crawler):
    if not crawler.facetlogfd:
        return
    facets = facet.compute_all('', '', '', resp_headers, [], [], url=url)
    geoip.add_facets(facets, host_geoip)
    if not isinstance(url, str):
        url = url.url
    facet_log = {'url': url, 'facets': facets, 'kind': kind, 'time': t}
    if seed_host:
        facet_log['seed_host'] = seed_host
    print(json.dumps(facet_log, sort_keys=True), file=crawler.facetlogfd)


'''
If we're robots blocked, the only 200 we're ever going to get is
for robots.txt. So, facet it.
'''


def post_robots_txt(f, url, host_geoip, seed_host, t, crawler):
    resp_headers = f.response.headers
    minimal_facet_me(resp_headers, url, host_geoip, seed_host, 'robots.txt', t, crawler)


'''
Study redirs at the host level to see if we're systematically getting
redirs from bare hostname to www or http to https, so we can do that
transformation in advance of the fetch.

Try to discover things that look like unknown url shorteners. Known
url shorteners should be treated as high-priority so that we can
capture the real underlying url before it has time to change, or for
the url shortener to go out of business.
'''


def handle_redirect(f, url, ridealong, priority, host_geoip, seed_host, json_log, crawler):
    resp_headers = f.response.headers
    minimal_facet_me(resp_headers, url, host_geoip, seed_host, 'redir', json_log['time'], crawler)

    location = resp_headers.get('location')
    if location is None:
        seeds.fail(ridealong, crawler)
        LOGGER.info('%d redirect for %s has no Location: header', f.response.status, url.url)
        raise ValueError(url.url + ' sent a redirect with no Location: header')
    next_url = urls.URL(location, urljoin=url)
    ridealong['url'] = next_url

    kind = urls.special_redirect(url, next_url)
    samesurt = url.surt == next_url.surt

    if 'seed' in ridealong:
        prefix = 'redirect seed'
    else:
        prefix = 'redirect'
    if kind is not None:
        stats.stats_sum(prefix+' '+kind, 1)
    else:
        stats.stats_sum(prefix+' non-special', 1)

    queue_next = True

    if kind is None:
        if samesurt:
            LOGGER.info('Whoops, %s is samesurt but not a special_redirect: %s to %s, location %s',
                        prefix, url.url, next_url.url, location)
    elif kind == 'same':
        LOGGER.info('attempted redirect to myself: %s to %s, location was %s', url.url, next_url.url, location)
        if 'Set-Cookie' not in resp_headers:
            LOGGER.info(prefix+' to myself and had no cookies.')
            stats.stats_sum(prefix+' same with set-cookie', 1)
        else:
            stats.stats_sum(prefix+' same without set-cookie', 1)
        seeds.fail(ridealong, crawler)
        queue_next = False
    else:
        LOGGER.info('special redirect of type %s for url %s', kind, url.url)
        # XXX push this info onto a last-k for the host
        # to be used pre-fetch to mutate urls we think will redir

    priority += 1

    if samesurt and kind != 'same':
        ridealong['skip_seen_url'] = True

    if 'freeredirs' in ridealong:
        priority -= 1
        json_log['freeredirs'] = ridealong['freeredirs']
        ridealong['freeredirs'] -= 1
        if ridealong['freeredirs'] == 0:
            del ridealong['freeredirs']
    ridealong['priority'] = priority

    if queue_next:
        crawler.add_url(priority, ridealong)

    json_log['redirect'] = next_url.url
    json_log['location'] = location
    if kind is not None:
        json_log['kind'] = kind
    if queue_next:
        json_log['found_new_links'] = 1
    else:
        json_log['found_new_links'] = 0

    # after we return, json_log will get logged


async def post_200(f, url, priority, host_geoip, seed_host, json_log, crawler):

    if crawler.warcwriter is not None:  # needs to use the same algo as post_dns for choosing what to warc
        # XXX insert the digest we already computed, instead of computing it again?
        crawler.warcwriter.write_request_response_pair(url.url, f.req_headers,
                                                       f.response.raw_headers, f.is_truncated, f.body_bytes)

    resp_headers = f.response.headers
    content_type = resp_headers.get('content-type', 'None')
    # sometimes content_type comes back multiline. whack it with a wrench.
    # XXX make sure I'm not creating blank lines and stopping cgi parse early?!
    content_type = content_type.replace('\r', '\n').partition('\n')[0]
    if content_type:
        content_type, _ = cgi.parse_header(content_type)
    else:
        content_type = 'Unknown'
    LOGGER.debug('url %r came back with content type %r', url.url, content_type)
    json_log['content_type'] = content_type
    stats.stats_sum('content-type=' + content_type, 1)
    if content_type == 'text/html':
        try:
            with stats.record_burn('response.text() decode', url=url):
                # need to guess at a reasonable encoding here
                # can't use the hidden algo from f.response.text() thanks to the use of streaming to limit bytes
                # hidden algo is: 1) consult headers, 2) if json, assume utf8, 3) call cchardet 4) assume utf8
                # XXX
                # let's not trust the headers too much!
                body = f.body_bytes.decode(encoding='utf8')
        except (UnicodeDecodeError, LookupError):
            # LookupError: .text() guessed an encoding that decode() won't understand (wut?)
            # XXX if encoding was in header, maybe I should use it here?
            body = f.body_bytes.decode(encoding='utf-8', errors='replace')

        # headers is a case-blind dict that's allergic to getting pickled.
        # let's make something more boring
        # XXX check that this is still a problem?
        # XXX use the one from warcio?
        resp_headers_list = []
        for k, v in resp_headers.items():
            resp_headers_list.append((k.lower(), v))

        if len(body) > int(config.read('Multiprocess', 'ParseInBurnerSize')):
            stats.stats_sum('parser in burner thread', 1)
            try:
                links, embeds, sha1, facets = await crawler.burner.burn(
                    partial(parse.do_burner_work_html, body, f.body_bytes, resp_headers_list,
                            burn_prefix='burner ', url=url),
                    url=url)
            except ValueError as e:  # if it pukes, we get back no values
                stats.stats_sum('parser raised', 1)
                LOGGER.info('parser raised %r', e)
                # XXX jsonlog
                return
        else:
            stats.stats_sum('parser in main thread', 1)
            try:
                # no coroutine state because this is a burn, not an await
                links, embeds, sha1, facets = parse.do_burner_work_html(
                    body, f.body_bytes, resp_headers_list, burn_prefix='main ', url=url)
            except ValueError:  # if it pukes, ..
                stats.stats_sum('parser raised', 1)
                # XXX jsonlog
                return
        json_log['checksum'] = sha1

        geoip.add_facets(facets, host_geoip)

        facet_log = {'url': url.url, 'facets': facets, 'kind': 'get'}
        facet_log['checksum'] = sha1
        facet_log['time'] = json_log['time']
        if seed_host:
            facet_log['seed_host'] = seed_host

        if crawler.facetlogfd:
            print(json.dumps(facet_log, sort_keys=True), file=crawler.facetlogfd)

        LOGGER.debug('parsing content of url %r returned %d links, %d embeds, %d facets',
                     url.url, len(links), len(embeds), len(facets))
        json_log['found_links'] = len(links) + len(embeds)
        stats.stats_max('max urls found on a page', len(links) + len(embeds))

        max_tries = config.read('Crawl', 'MaxTries')

        new_links = 0
        for u in links:
            ridealong = {'url': u, 'priority': priority+1, 'retries_left': max_tries}
            if crawler.add_url(priority + 1, ridealong):
                new_links += 1
        for u in embeds:
            ridealong = {'url': u, 'priority': priority-1, 'retries_left': max_tries}
            if crawler.add_url(priority - 1, ridealong):
                new_links += 1

        if new_links:
            json_log['found_new_links'] = new_links

        # XXX process meta-http-equiv-refresh

        # XXX plugin for links and new links - post to Kafka, etc
        # neah stick that in add_url!

        # actual jsonlog is emitted after the return


def post_dns(dns, expires, url, crawler):
    if crawler.warcwriter is not None:  # needs to use the same algo as post_200 for choosing what to warc
        crawler.warcwriter.write_dns(dns, expires, url)
