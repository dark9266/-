"""튠(tune.kr) 크롤러.

Shopify Storefront GraphQL API (GET 전용).
variant title에서 모델번호/사이즈 추출.
"""

from __future__ import annotations

import re
from datetime import datetime

import httpx

from src.crawlers.registry import register
from src.models.product import RetailProduct, RetailSizeInfo
from src.utils.logging import setup_logger
from src.utils.rate_limiter import AsyncRateLimiter

logger = setup_logger("tune_crawler")

STOREFRONT_URL = "https://tuneglobal.myshopify.com/api/2025-10/graphql.json"
ACCESS_TOKEN = "fafc2c72f352b1cfcf3bea801ab3c7fd"
WEB_BASE = "https://tune.kr"

HEADERS = {
    "X-Shopify-Storefront-Access-Token": ACCESS_TOKEN,
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

# GraphQL 쿼리: 검색
_SEARCH_QUERY = """\
{
  products(first: %d, query: "%s") {
    edges {
      node {
        id
        title
        handle
        vendor
        metafields(identifiers: [{namespace: "custom", key: "hidden"}]) {
          key
          value
        }
        variants(first: 50) {
          edges {
            node {
              title
              price { amount currencyCode }
              compareAtPrice { amount }
              availableForSale
              quantityAvailable
            }
          }
        }
        images(first: 1) {
          edges {
            node { url }
          }
        }
      }
    }
  }
}"""

# GraphQL 쿼리: 단일 상품 (ID 기반)
_PRODUCT_BY_ID_QUERY = """\
{
  node(id: "gid://shopify/Product/%s") {
    ... on Product {
      id
      title
      handle
      vendor
      metafields(identifiers: [{namespace: "custom", key: "hidden"}]) {
        key
        value
      }
      variants(first: 50) {
        edges {
          node {
            title
            price { amount currencyCode }
            compareAtPrice { amount }
            availableForSale
            quantityAvailable
          }
        }
      }
      images(first: 1) {
        edges {
          node { url }
        }
      }
    }
  }
}"""


# --------------- 순수 파싱 함수 ---------------


def _parse_variant_title(title: str) -> tuple[str, str]:
    """variant title → (모델번호, 사이즈).

    예: "IQ3446-010 / 230" → ("IQ3446-010", "230")
         "IQ3446-010"      → ("IQ3446-010", "")
         ""                → ("", "")
    """
    if not title:
        return ("", "")
    parts = title.split(" / ", maxsplit=1)
    model = parts[0].strip()
    size = parts[1].strip() if len(parts) > 1 else ""
    return (model, size)


def _is_hidden(product_node: dict) -> bool:
    """metafields에서 custom.hidden == "true" 여부 확인."""
    metafields = product_node.get("metafields") or []
    for mf in metafields:
        if mf is None:
            continue
        if isinstance(mf, dict) and mf.get("key") == "hidden" and mf.get("value") == "true":
            return True
    return False


def _extract_product_id(gid: str) -> str:
    """GraphQL GID에서 숫자 ID 추출.

    "gid://shopify/Product/12345" → "12345"
    "12345" → "12345"
    """
    if "/" in gid:
        return gid.rsplit("/", 1)[-1]
    return gid


def _parse_product_node(node: dict) -> dict | None:
    """GraphQL product node → 표준 search result dict.

    hidden 상품이면 None 반환.
    """
    if _is_hidden(node):
        return None

    product_id = _extract_product_id(node.get("id") or "")
    title = node.get("title") or ""
    handle = node.get("handle") or ""
    vendor = node.get("vendor") or ""

    # 이미지
    image_edges = (node.get("images") or {}).get("edges") or []
    image_url = image_edges[0]["node"]["url"] if image_edges else ""

    # variants 파싱
    variant_edges = (node.get("variants") or {}).get("edges") or []
    sizes: list[dict] = []
    model_number = ""
    first_price = 0
    first_original = 0
    all_sold_out = True

    for vedge in variant_edges:
        vnode = vedge.get("node") or {}
        vtitle = vnode.get("title") or ""
        vmodel, vsize = _parse_variant_title(vtitle)

        if not model_number and vmodel:
            model_number = vmodel

        price_amount = vnode.get("price") or {}
        price = int(float(price_amount.get("amount") or "0"))

        compare_at = vnode.get("compareAtPrice")
        if compare_at and isinstance(compare_at, dict):
            original_price = int(float(compare_at.get("amount") or "0"))
        else:
            original_price = price

        if not first_price and price:
            first_price = price
            first_original = original_price

        available = bool(vnode.get("availableForSale", False))
        qty = vnode.get("quantityAvailable") or 0

        if available:
            all_sold_out = False

        sizes.append({
            "size": vsize,
            "price": price,
            "original_price": original_price,
            "in_stock": available,
            "quantity": qty,
        })

    return {
        "product_id": product_id,
        "name": title,
        "brand": vendor,
        "model_number": model_number,
        "price": first_price,
        "original_price": first_original,
        "url": f"{WEB_BASE}/products/{handle}" if handle else "",
        "image_url": image_url,
        "is_sold_out": all_sold_out if variant_edges else True,
        "sizes": sizes,
    }


def _escape_graphql_string(s: str) -> str:
    """GraphQL 문자열 내부에 삽입할 값 이스케이프."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# --------------- 크롤러 클래스 ---------------


class TuneCrawler:
    """튠(tune.kr) 크롤러 — Shopify Storefront GraphQL API."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._rate_limiter = AsyncRateLimiter(max_concurrent=2, min_interval=1.0)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=HEADERS,
                timeout=15.0,
                follow_redirects=True,
                http2=False,
            )
        return self._client

    async def search_products(self, keyword: str, limit: int = 30) -> list[dict]:
        """키워드로 상품 검색 (GraphQL GET)."""
        keyword = (keyword or "").strip()
        if not keyword:
            return []

        escaped = _escape_graphql_string(keyword)
        query = _SEARCH_QUERY % (limit, escaped)

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(STOREFRONT_URL, params={"query": query})

        if resp.status_code != 200:
            logger.warning("Tune 검색 HTTP %d (keyword=%s)", resp.status_code, keyword)
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Tune 검색 JSON 파싱 실패 (keyword=%s)", keyword)
            return []

        edges = (data.get("data") or {}).get("products", {}).get("edges") or []
        results: list[dict] = []
        for edge in edges:
            node = edge.get("node")
            if not node:
                continue
            parsed = _parse_product_node(node)
            if parsed is not None:
                results.append(parsed)

        logger.info("Tune 검색 '%s': %d건", keyword, len(results))
        return results

    async def get_product_detail(self, product_id: str) -> RetailProduct | None:
        """상품 상세 조회 (GraphQL GET)."""
        if not product_id:
            return None

        query = _PRODUCT_BY_ID_QUERY % product_id

        client = await self._get_client()
        async with self._rate_limiter.acquire():
            resp = await client.get(STOREFRONT_URL, params={"query": query})

        if resp.status_code != 200:
            logger.warning("Tune 상세 HTTP %d (id=%s)", resp.status_code, product_id)
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        node = (data.get("data") or {}).get("node")
        if not node:
            return None

        parsed = _parse_product_node(node)
        if parsed is None:
            return None

        # RetailSizeInfo 변환 (재고 있는 사이즈만)
        sizes: list[RetailSizeInfo] = []
        for s in parsed.get("sizes") or []:
            if not s["in_stock"]:
                continue
            opt_price = s["price"]
            orig = s["original_price"] or opt_price
            discount_rate = 0.0
            if orig and opt_price and orig > opt_price:
                discount_rate = round(1 - opt_price / orig, 3)
            sizes.append(
                RetailSizeInfo(
                    size=s["size"],
                    price=opt_price,
                    original_price=orig,
                    in_stock=True,
                    discount_type="할인" if discount_rate > 0 else "",
                    discount_rate=discount_rate,
                )
            )

        product = RetailProduct(
            source="tune",
            product_id=parsed["product_id"],
            name=parsed["name"],
            model_number=parsed["model_number"],
            brand=parsed["brand"],
            url=parsed["url"],
            image_url=parsed["image_url"],
            sizes=sizes,
            fetched_at=datetime.now(),
        )
        logger.info(
            "Tune 상품: %s | 모델: %s | %s원 | 사이즈: %d개",
            parsed["name"],
            parsed["model_number"],
            f"{parsed['price']:,}" if parsed["price"] else "?",
            len(sizes),
        )
        return product

    async def disconnect(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("튠 크롤러 연결 해제")


# 싱글톤 + 레지스트리 등록
tune_crawler = TuneCrawler()
register("tune", tune_crawler, "튠")
