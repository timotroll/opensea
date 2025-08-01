"""
Script to fetch top NFT collections from OpenSea via their GraphQL API,
process a fixed number of result pages and export summary statistics to
JSON. The program reads page cursors from ``cursor.txt`` (one per line)
and iterates through a configurable number of pages, requesting up to
100 items per page. For each collection it extracts the collection
name, floor price (USD if available, otherwise native token value),
top offer (USD or native), calculates the percentage difference
between the floor price and the top offer, and constructs a link to
the collection on OpenSea.

To adjust how many pages are processed you can either set the
``NUM_PAGES`` constant below or export an environment variable
``NUM_PAGES`` before running the script. The default is 10 pages.

Results are written to ``output.json`` in the current working
directory.
"""
import time
import asyncio
import json
import logging
import os
import random
from typing import Any, Dict, List, Optional

# Third‑party library used for generating realistic User‑Agent strings. If this
# import fails you may need to install it via `pip install fake-useragent`.
try:
    from fake_useragent import UserAgent  # type: ignore
except ImportError as exc:
    raise ImportError(
        "fake_useragent is required for user agent rotation. Install it via 'pip install fake-useragent'"
    ) from exc

try:
    import tls_client  # type: ignore
except ImportError as exc:
    raise ImportError(
        "tls_client is required for this script. Install it via 'pip install tls-client'"
    ) from exc

BASE_URL_GRAPHQL = "https://gql.opensea.io/graphql"

# Configure how many pages to process. Default to 10 unless overridden
# via environment variable NUM_PAGES.
NUM_PAGES: int = int(os.getenv("NUM_PAGES", "30"))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class OpenSeaClient:
    """HTTP client wrapper around tls_client.Session with UA/Proxy support."""

    def __init__(self, user_agent: Optional[str] = None, proxy: Optional[str] = None) -> None:
        """
        Initialise the client with an optional user agent and proxy.

        Parameters
        ----------
        user_agent: Optional[str]
            A custom User‑Agent string. If provided it will override the
            default UA. If ``None``, the default UA remains.
        proxy: Optional[str]
            Proxy string in the format accepted by tls_client (for example
            ``http://user:pass@host:port`` or ``http://host:port``). If
            provided, requests will be routed through this proxy. If
            ``None``, no proxy is used.
        """
        self.session = tls_client.Session(
            client_identifier="chrome_120",
            random_tls_extension_order=True,
        )
        # default headers
        self.headers = {
            "accept": "application/json",
            "User-Agent": user_agent or "Mozilla/5.0",
        }
        self.proxy = proxy

    def _sync_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        # prepare kwargs for proxy if configured
        kwargs: Dict[str, Any] = {}
        if self.proxy:
            # tls_client supports passing proxy as a keyword argument
            kwargs["proxy"] = self.proxy
        if method == "GET":
            response = self.session.get(
                url, headers=self.headers, params=params, **kwargs
            )
        else:
            response = self.session.post(
                url, headers=self.headers, json=json_payload, **kwargs
            )
        if response.status_code >= 400:
            raise Exception(
                f"Error {response.status_code} on {url}: {response.text}"
            )
        return response.json()

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None,
    ) -> Any:
        # Offload synchronous request to a thread to avoid blocking the event loop
        return await asyncio.to_thread(
            self._sync_request, method, url, params, json_payload
        )


async def fetch_page_collections(
    client: OpenSeaClient, cursor: Optional[str], limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Fetch a single page of top collections from OpenSea's GraphQL API.

    Parameters
    ----------
    client: OpenSeaClient
        HTTP client instance used to perform requests.
    cursor: Optional[str]
        Pagination cursor. Pass ``None`` to retrieve the first page.
    limit: int
        Number of items to request per page. Maximum supported by the
        endpoint appears to be 100.

    Returns
    -------
    List[Dict[str, Any]]
        Raw collection objects returned by the GraphQL API.
    """
    # GraphQL query extracted from the OpenSea web app. It requests a
    # variety of fields including statistics, floor price, top offer and
    # token information. Updating this string to reflect changes in
    # OpenSea's schema may be necessary over time.
    query = '''
    query TopStatsTableQuery($cursor: String, $sort: TopCollectionsSort!, $filter: TopCollectionsFilter, $category: CategoryIdentifier, $limit: Int!) {
      topCollections(
        cursor: $cursor
        sort: $sort
        filter: $filter
        category: $category
        limit: $limit
      ) {
        items {
          id
          slug
          __typename
          ...StatsVolume
          ...StatsTableRow
          ...CollectionStatsSubscription
          ...CollectionNativeCurrencyIdentifier
        }
        nextPageCursor
        __typename
      }
    }
    fragment StatsVolume on Collection {
      stats {
        volume {
          native {
            unit
            __typename
          }
          ...Volume
          __typename
        }
        oneMinute {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        fifteenMinute {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        fiveMinute {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        oneDay {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        oneHour {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        sevenDays {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        thirtyDays {
          volume {
            native {
              unit
              __typename
            }
            ...Volume
            __typename
          }
          __typename
        }
        __typename
      }
      __typename
    }
    fragment Volume on Volume {
      usd
      native {
        symbol
        unit
        __typename
      }
      __typename
    }
    fragment StatsTableRow on Collection {
      id
      slug
      ...StatsTableRowFloorPrice
      ...StatsTableRowTopOffer
      ...StatsTableRowFloorChange
      ...StatsTableRowOwners
      ...StatsTableRowSales
      ...StatsTableRowSupply
      ...StatsTableRowVolume
      ...StatsTableRowCollection
      ...isRecentlyMinted
      ...CollectionLink
      ...CollectionPreviewTooltip
      ...CollectionWatchListButton
      ...StatsTableRowSparkLineChart
      ...StatsTableRowFloorPriceMobile
      __typename
    }
    fragment isRecentlyMinted on Collection {
      createdAt
      __typename
    }
    fragment CollectionLink on CollectionIdentifier {
      slug
      ... on Collection {
        ...getDropStatus
        __typename
      }
      __typename
    }
    fragment getDropStatus on Collection {
      drop {
        __typename
        ... on Erc721SeaDropV1 {
          maxSupply
          totalSupply
          __typename
        }
        ... on Erc1155SeaDropV2 {
          tokenSupply {
            totalSupply
            maxSupply
            __typename
          }
          __typename
        }
        stages {
          startTime
          endTime
          __typename
        }
      }
      __typename
    }
    fragment StatsTableRowFloorPrice on Collection {
      floorPrice {
        pricePerItem {
          token {
            unit
            __typename
          }
          ...TokenPrice
          __typename
        }
        __typename
      }
      __typename
    }
    fragment TokenPrice on Price {
      usd
      token {
        unit
        symbol
        contractAddress
        chain {
          identifier
          __typename
        }
        __typename
      }
      __typename
    }
    fragment StatsTableRowTopOffer on Collection {
      topOffer {
        pricePerItem {
          token {
            unit
            __typename
          }
          ...TokenPrice
          __typename
        }
        __typename
      }
      __typename
    }
    fragment StatsTableRowFloorChange on Collection {
      stats {
        oneMinute {
          floorPriceChange
          __typename
        }
        fiveMinute {
          floorPriceChange
          __typename
        }
        fifteenMinute {
          floorPriceChange
          __typename
        }
        oneDay {
          floorPriceChange
          __typename
        }
        oneHour {
          floorPriceChange
          __typename
        }
        sevenDays {
          floorPriceChange
          __typename
        }
        thirtyDays {
          floorPriceChange
          __typename
        }
        __typename
      }
      __typename
    }
    fragment StatsTableRowOwners on Collection {
      stats {
        ownerCount
        __typename
      }
      __typename
    }
    fragment StatsTableRowSales on Collection {
      stats {
        sales
        oneMinute {
          sales
          __typename
        }
        fiveMinute {
          sales
          __typename
        }
        fifteenMinute {
          sales
          __typename
        }
        oneDay {
          sales
          __typename
        }
        oneHour {
          sales
          __typename
        }
        sevenDays {
          sales
          __typename
        }
        thirtyDays {
          sales
          __typename
        }
        __typename
      }
      __typename
    }
    fragment StatsTableRowSupply on Collection {
      stats {
        totalSupply
        __typename
      }
      __typename
    }
    fragment StatsTableRowVolume on Collection {
      ...StatsVolume
      __typename
    }
    fragment StatsTableRowCollection on Collection {
      name
      isVerified
      ...CollectionImage
      ...NewCollectionChip
      ...CollectionPreviewTooltip
      ...isRecentlyMinted
      __typename
    }
    fragment CollectionPreviewTooltip on CollectionIdentifier {
      ...CollectionPreviewTooltipContent
      __typename
    }
    fragment CollectionPreviewTooltipContent on CollectionIdentifier {
      slug
      __typename
    }
    fragment CollectionImage on Collection {
      name
      imageUrl
      chain {
        ...ChainBadge
        __typename
      }
      __typename
    }
    fragment ChainBadge on Chain {
      identifier
      name
      __typename
    }
    fragment NewCollectionChip on Collection {
      createdAt
      ...isRecentlyMinted
      __typename
    }
    fragment CollectionWatchListButton on Collection {
      slug
      name
      __typename
    }
    fragment StatsTableRowSparkLineChart on Collection {
      ...FloorPriceSparkLineChart
      __typename
    }
    fragment FloorPriceSparkLineChart on Collection {
      analytics {
        sparkLineSevenDay {
          price {
            token {
              unit
              symbol
              __typename
            }
            __typename
          }
          time
          __typename
        }
        __typename
      }
      __typename
    }
    fragment StatsTableRowFloorPriceMobile on Collection {
      ...StatsTableRowFloorPrice
      ...StatsTableRowFloorChange
      __typename
    }
    fragment CollectionStatsSubscription on Collection {
      id
      slug
      __typename
      floorPrice {
        pricePerItem {
          usd
          ...TokenPrice
          ...NativePrice
          __typename
        }
        __typename
      }
      topOffer {
        pricePerItem {
          usd
          ...TokenPrice
          ...NativePrice
          __typename
        }
        __typename
      }
      stats {
        ownerCount
        totalSupply
        uniqueItemCount
        listedItemCount
        volume {
          usd
          ...Volume
          __typename
        }
        sales
        oneMinute {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        fiveMinute {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        fifteenMinute {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        oneHour {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        oneDay {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        sevenDays {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        thirtyDays {
          floorPriceChange
          sales
          volume {
            usd
            ...Volume
            __typename
          }
          __typename
        }
        __typename
      }
    }
    fragment NativePrice on Price {
      ...UsdPrice
      token {
        unit
        contractAddress
        ...currencyIdentifier
        __typename
      }
      native {
        symbol
        unit
        contractAddress
        ...currencyIdentifier
        __typename
      }
      __typename
    }
    fragment UsdPrice on Price {
      usd
      token {
        contractAddress
        unit
        ...currencyIdentifier
        __typename
      }
      __typename
    }
    fragment currencyIdentifier on ContractIdentifier {
      contractAddress
      chain {
        identifier
        __typename
      }
      __typename
    }
    fragment CollectionNativeCurrencyIdentifier on Collection {
      chain {
        identifier
        nativeCurrency {
          address
          __typename
        }
        __typename
      }
      __typename
    }
    '''

    # Prepare GraphQL variables
    base_vars: Dict[str, Any] = {
        "sort": {"by": "ONE_DAY_VOLUME", "direction": "DESC"},
        "filter": None,
        "category": None,
    }
    variables = {**base_vars, "cursor": cursor, "limit": limit}
    payload = {"query": query, "variables": variables}
    logger.debug(f"Requesting collections page cursor={cursor}")
    response = await client.request("POST", BASE_URL_GRAPHQL, json_payload=payload)
    items = (
        response.get("data", {})
        .get("topCollections", {})
        .get("items", [])
        or []
    )
    return items


def extract_pricing(item: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Extract floor price and top offer from a collection item in both USD and ETH.

    The OpenSea API provides prices denominated in USD ("usd") and the
    collection's native token ("native"). For the purposes of the final
    report we need the floor price expressed in USD (to be returned as
    ``price``) and both the listing (floor) and offer prices expressed in
    ETH. Missing values are represented as ``None``.

    Returns
    -------
    dict
        A mapping with keys ``usd_floor``, ``eth_floor`` and
        ``eth_offer``.
    """
    usd_floor: Optional[float] = None
    eth_floor: Optional[float] = None
    eth_offer: Optional[float] = None

    # Floor price extraction
    floor = item.get("floorPrice")
    if floor:
        price_per_item = floor.get("pricePerItem")
        if price_per_item:
            # USD representation of the floor price
            if price_per_item.get("usd") is not None:
                try:
                    usd_floor = float(price_per_item["usd"])
                except (TypeError, ValueError):
                    usd_floor = None
            # ETH (native) representation of the floor price
            native = price_per_item.get("native")
            if native and native.get("symbol") == "ETH" and native.get("unit") is not None:
                try:
                    eth_floor = float(native["unit"])
                except (TypeError, ValueError):
                    eth_floor = None

    # Top offer extraction
    top = item.get("topOffer")
    if top:
        price_per_item = top.get("pricePerItem")
        if price_per_item:
            native = price_per_item.get("native")
            if native and native.get("symbol") == "ETH" and native.get("unit") is not None:
                try:
                    eth_offer = float(native["unit"])
                except (TypeError, ValueError):
                    eth_offer = None

    return {"usd_floor": usd_floor, "eth_floor": eth_floor, "eth_offer": eth_offer}


def calculate_difference(floor_eth: Optional[float], offer_eth: Optional[float]) -> Optional[float]:
    """
    Compute the percentage difference between floor and offer prices using ETH values.

    Parameters
    ----------
    floor_eth: Optional[float]
        Floor price expressed in ETH.
    offer_eth: Optional[float]
        Top offer price expressed in ETH.

    Returns
    -------
    Optional[float]
        The percentage difference ((floor - offer) / floor) * 100 or ``None``
        if either value is missing or zero.
    """
    if floor_eth is None or offer_eth is None or floor_eth == 0:
        return None
    return ((floor_eth - offer_eth) / floor_eth) * 100.0


async def main() -> None:
    client = OpenSeaClient()
    # Read pagination cursors from file. Each non-empty line after
    # page 1 corresponds to subsequent pages (page 2, page 3, ...). The
    # first page is always requested with a ``None`` cursor.
    try:
        with open("cursor.txt", "r", encoding="utf-8") as f:
            file_cursors = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.error(
            "cursor.txt not found. Please create this file with one cursor ID per line."
        )
        return

    # Construct the list of cursors to process: first entry is None (for page 1),
    # followed by identifiers for pages 2..N.
    all_cursors: List[Optional[str]] = [None] + file_cursors
    total_available_pages = len(all_cursors)
    logger.info(
        f"Processing up to {NUM_PAGES} pages (available pages including first: {total_available_pages})"
    )

    # Prepare proxy list if proxy.txt exists
    proxies: List[str] = []
    try:
        with open("proxy.txt", "r", encoding="utf-8") as pf:
            proxies = [line.strip() for line in pf if line.strip()]
        if proxies:
            logger.info(f"Loaded {len(proxies)} proxies from proxy.txt")
    except FileNotFoundError:
        logger.info("proxy.txt not found. Proceeding without proxy rotation.")

    # Initialise user agent generator
    ua_generator = UserAgent()

    # Semaphore to limit concurrent requests to 10
    semaphore = asyncio.Semaphore(30)
    start_time = time.monotonic() 
    async def process_page(page_index: int, cursor: Optional[str]) -> List[Dict[str, Any]]:
        """Process a single page: fetch data and transform into result entries."""
        async with semaphore:
            # Generate a realistic desktop user agent
            user_agent = ua_generator.random
            # If proxies are available select one at random, prefixing with scheme if needed
            proxy = None
            if proxies:
                raw_proxy = random.choice(proxies)
                if raw_proxy and not raw_proxy.startswith(("http://", "https://")):
                    proxy = f"http://{raw_proxy}"
                else:
                    proxy = raw_proxy
            # Create a new client for this page with random UA and proxy
            page_client = OpenSeaClient(user_agent=user_agent, proxy=proxy)
            try:
                items = await fetch_page_collections(page_client, cursor, limit=100)
                logger.info(
                    f"Fetched {len(items)} collections from page {page_index + 1}"
                )
            except Exception as exc:
                logger.error(
                    f"Error fetching page {page_index + 1} (cursor={cursor}): {exc}"
                )
                return []
            page_results: List[Dict[str, Any]] = []
            for item in items:
                name = item.get("name") or item.get("slug") or "Unknown Collection"
                slug = item.get("slug")
                link = f"https://opensea.io/collection/{slug}" if slug else None
                pricing = extract_pricing(item)
                # Calculate difference using ETH values
                diff = calculate_difference(pricing["eth_floor"], pricing["eth_offer"])
                page_results.append(
                    {
                        "collection": name,
                        "price": pricing["usd_floor"],
                        "list": pricing["eth_floor"],
                        "offer": pricing["eth_offer"],
                        "difference_percent": diff,
                        "link": link,
                    }
                )
            return page_results

    # Launch tasks concurrently up to NUM_PAGES
    tasks = [
        asyncio.create_task(process_page(idx, cursor))
        for idx, cursor in enumerate(all_cursors[:NUM_PAGES])
    ]
    # Gather results
    results: List[Dict[str, Any]] = []
    for page_result in await asyncio.gather(*tasks):
        results.extend(page_result)
    elapsed = time.monotonic() - start_time
    logger.info(f"Elapsed time: {elapsed:.2f} seconds")
    # Write out the results

if __name__ == "__main__":
    asyncio.run(main())