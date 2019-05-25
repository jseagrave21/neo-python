import asyncio
import aiohttp
import json
import socket

from neo.Network.neonetwork.network.ipfilter import IPFilter
from neo.Network.neonetwork.network import utils as networkutils

"""
 A class for creating a dynamic seedlist for use with Safemode.
"""


class DynamicSeedlist():
    def init(self):
        self.ipfilter = IPFilter()
        self.reset_ipfilter_config()

    def reset_ipfilter_config(self):
        self.ipfilter.config = {
            'blacklist': [
                '0.0.0.0/0'
            ],
            'whitelist': [
            ]
        }

    async def mainnet_build(self):
        await self.build('https://raw.githubusercontent.com/CityOfZion/neo-mon/master/docs/assets/mainnet.json', "10332", "10333")

    async def testnet_build(self):
        await self.build('https://raw.githubusercontent.com/CityOfZion/neo-mon/master/docs/assets/testnet.json', "20332", "20333")

    async def build(self, raw_seedlist, http_port, P2P_port):
        async with aiohttp.ClientSession() as session:
            async with session.get(raw_seedlist) as res:
                data = await res.json(content_type=None)
        sites = data['sites']
        results = await asyncio.gather(*[filtersite(site, http_port) for site in sites], return_exceptions=True)

        site_dict = {}
        for i in results:
            if type(i) == dict:
                site_dict.update(i)

        heights = sorted(site_dict.values(), reverse=True)
        threshold = heights[0] - 2  # gives 30 sec on average to complete the query
        to_remove = []
        for key, val in site_dict.items():
            if val < threshold:
                to_remove.append(key)
        for site in to_remove:
            del site_dict[site]
        self.ipfilter.config[""]
        for key in site_dict.keys():
            if not networkutils.is_ip_address(key):
                try:
                    key = networkutils.hostname_to_ip(key)
                except socket.gaierror:
                    continue
            self.ipfilter.whitelist_add(key + ":" + P2P_port)


"""
internal functions
"""


def body(method):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method, 
        "params": []
    }


async def filtersite(site, http_port):
    if site['type'] == "RPC":
        if site['protocol'] == "https":
            port = ":" + site['port']
        elif site['protocol'] == "http":
            port = ":" + http_port
        url = site['protocol'] + '://' + site['url'] + port
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url=url, json=body("getversion")) as res:
                    version = await res.json()
        except aiohttp.ClientError as e:
            return e
        if "NEO-PYTHON" not in version['result']['useragent']:  # NEO-PYTHON 0.8.4+ employs SimplePolicy
            async with aiohttp.ClientSession() as session:
                async with session.post(url=url, json=body("listplugins")) as res:
                    plugins = await res.json()
            p_list = []
            try:
                for p in plugins['result']:
                    p_list.append(p['name'])
            except KeyError:
                return 1
            if "SimplePolicyPlugin" not in p_list:
                return 2
        async with aiohttp.ClientSession() as session:
            async with session.post(url=url, json=body("getblockcount")) as res:
                blockheight = await res.json()
        return {site['url']: blockheight['result']}
