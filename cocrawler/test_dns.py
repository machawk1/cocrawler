'''
This test does talk to the network... that might be a little surprising
for something that purports to be a unit test.
'''

import socket
import asyncio
import urllib
import logging

import aiohttp
import pytest

import dns

levels = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG]
logging.basicConfig(level=levels[3])

ns = ['8.8.8.8', '8.8.4.4'] # google
resolver = aiohttp.resolver.AsyncResolver(nameservers=ns)

@pytest.mark.asyncio
async def test_prefetch_dns():
    url = 'http://google.com/'
    parts = urllib.parse.urlparse(url)
    mock_url = None
    resolver = aiohttp.resolver.AsyncResolver(nameservers=ns)
    connector = aiohttp.connector.TCPConnector(resolver=resolver, family=socket.AF_INET)
    session = aiohttp.ClientSession(connector=connector)

    # whew

    iplist = await dns.prefetch_dns(parts, mock_url, session)

    assert len(iplist) > 0

@pytest.mark.asyncio
async def test_resolver():
    dns.setup_resolver(ns)

    iplist = await dns.query('google.com', 'A')
    assert len(iplist) > 0

    iplist = await dns.query('google.com', 'AAAA')
    assert len(iplist) > 0

    iplist = await dns.query('google.com', 'NS')
    assert len(iplist) > 0

    iplist = await dns.query('google.com', 'CNAME')
    assert iplist == None

    iplist = await dns.query('www.blogger.com', 'CNAME')
    assert len(iplist) > 0
