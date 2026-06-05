    async def close_position(
        self, market_slug: str, side: str, price: float, size_usd: float
    ) -> dict | None:
        if not self._us_client:
            logger.error("Polymarket.US client not initialised — cannot close position")
            return None
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._sync_close_position, market_slug, side, price, size_usd
            )
        except Exception as exc:
            logger.error("close_position failed for %s: %s", market_slug, exc)
            return None

    def _sync_close_position(
        self, market_slug: str, side: str, price: float, size_usd: float
    ) -> dict | None:
        from polymarket_us import AuthenticationError, BadRequestError, NotFoundError
        intent   = "ORDER_INTENT_SELL_LONG" if side == "YES" else "ORDER_INTENT_SELL_SHORT"
        quantity = max(1, round(size_usd / price))
        order = {
            "marketSlug": market_slug,
            "intent":     intent,
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": str(round(price, 4)), "currency": "USD"},
            "quantity":   quantity,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        logger.info("Closing position: %s", order)
        try:
            resp = self._us_client.orders.create(order)
            logger.info("Close response: %s", resp)
            return resp
        except AuthenticationError as exc:
            logger.error("Auth error closing %s: %s", market_slug, exc)
        except BadRequestError as exc:
            logger.error("Bad request closing %s: %s", market_slug, exc)
        except NotFoundError as exc:
            logger.error("Market not found closing %s: %s", market_slug, exc)
        except Exception as exc:
            logger.error("Close order error for %s: %s", market_slug, exc)
        return None

    # ---------------------------------------------------------------- #
    # Helpers                                                           #
    # ---------------------------------------------------------------- #

    def resolve_token_id(self, market, side):
        tokens = market.get("clobTokenIds") or market.get("tokens") or []
