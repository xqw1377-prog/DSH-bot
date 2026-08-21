import io

p = "services/quant-gateway/src/quant_gateway/routers/orders.py"
src = io.open(p, encoding="utf-8").read()

old = '''        if previous_order_id:
            raise structured_error(
                409,
                error_code="DUPLICATE_ORDER",
                phase="PRE_SUBMIT",
                retryable=False,
                submission_unknown=False,
                message=(
                    "duplicate idempotency key; no new order created; "
                    f"previous order_id={previous_order_id}"
                ),
            )
        # SUBMISSION_UNKNOWN：venue 可能已接单但网关在写库前崩溃。'''
new = '''        if previous_order_id:
            raise structured_error(
                409,
                error_code="DUPLICATE_ORDER",
                phase="PRE_SUBMIT",
                retryable=False,
                submission_unknown=False,
                message=(
                    "duplicate idempotency key; no new order created; "
                    f"previous order_id={previous_order_id}"
                ),
            )
        record = storage.get_idempotency_record(idempotency_key) or {}
        if record.get("status") == "FAILED":
            # 确定性失败(如 venue 明确拒单后释放):不在途、无未知风险,
            # 立即重走完整门禁(claim 内会清理并重新抢占该键)
            pass
        else:
            # SUBMISSION_UNKNOWN：venue 可能已接单但网关在写库前崩溃。'''
assert old in src, "anchor 1 not found"
src = src.replace(old, new)

old_block = '''            # SUBMISSION_UNKNOWN：venue 可能已接单但网关在写库前崩溃。
            # 仅当键已老化（超过在途窗口）才做恢复：新鲜的 RESERVED 属于并发在途，
            # venue 查不到不代表未接单。恢复顺序：找到即认领；明确不存在才释放重试。
        record = storage.get_idempotency_record(idempotency_key) or {}
        age_seconds = _idempotency_age_seconds(record.get("updated_at"))
        if age_seconds is None or age_seconds < SUBMISSION_UNKNOWN_GRACE_SECONDS:
            raise structured_error(
                409,
                error_code="IDEMPOTENCY_IN_FLIGHT",
                phase="SUBMITTING",
                retryable=True,
                submission_unknown=True,
                message=(
                    "idempotency key in flight (RESERVED without order_id); "
                    "retry after grace period"
                ),
            )
        recovered = adapter.find_order_by_idempotency_key(idempotency_key)
        if recovered is not None and recovered.get("order_id"):
            recovered_id = recovered["order_id"]
            storage.finalize_idempotency_key(idempotency_key, recovered_id)
            audit.record(
                "order.submission_recovered",
                service_principal=principal.name,
                market=market.value,
                subject_id=recovered_id,
                detail=f"intent={idempotency_key} recovered from venue",
            )
            return {"order_id": recovered_id, "status": "SUBMITTED",
                    "recovered": True}
        if recovered is None:
            consistency = getattr(adapter, "order_lookup_consistency", "UNSUPPORTED")
            if consistency != "STRONG":
                raise structured_error(
                    409,
                    error_code="SUBMISSION_UNKNOWN",
                    phase="SUBMITTING",
                    retryable=False,
                    submission_unknown=True,
                    message=(
                        "submission unknown: venue lookup is not strongly consistent; "
                        "resubmission blocked"
                    ),
                )
            # STRONG：venue 确认从未接受，释放幂等键后按新单继续走完整门禁
            storage.mark_idempotency_failed(idempotency_key)
        else:
            # 查询结果异常（无 order_id 的记录）：保持占用，禁止重提
            raise structured_error(
                409,
                error_code="SUBMISSION_UNKNOWN",
                phase="SUBMITTING",
                retryable=False,
                submission_unknown=True,
                message=(
                    "submission unknown: idempotency key occupied without "
                    "order_id; venue lookup inconclusive; resubmission blocked"
                ),
            )'''
new_block = '''            # 仅当键已老化（超过在途窗口）才做恢复：新鲜的 RESERVED 属于并发在途，
            # venue 查不到不代表未接单。恢复顺序：找到即认领；明确不存在才释放重试。
            age_seconds = _idempotency_age_seconds(record.get("updated_at"))
            if age_seconds is None or age_seconds < SUBMISSION_UNKNOWN_GRACE_SECONDS:
                raise structured_error(
                    409,
                    error_code="IDEMPOTENCY_IN_FLIGHT",
                    phase="SUBMITTING",
                    retryable=True,
                    submission_unknown=True,
                    message=(
                        "idempotency key in flight (RESERVED without order_id); "
                        "retry after grace period"
                    ),
                )
            recovered = adapter.find_order_by_idempotency_key(idempotency_key)
            if recovered is not None and recovered.get("order_id"):
                recovered_id = recovered["order_id"]
                storage.finalize_idempotency_key(idempotency_key, recovered_id)
                audit.record(
                    "order.submission_recovered",
                    service_principal=principal.name,
                    market=market.value,
                    subject_id=recovered_id,
                    detail=f"intent={idempotency_key} recovered from venue",
                )
                return {"order_id": recovered_id, "status": "SUBMITTED",
                        "recovered": True}
            if recovered is None:
                consistency = getattr(adapter, "order_lookup_consistency", "UNSUPPORTED")
                if consistency != "STRONG":
                    raise structured_error(
                        409,
                        error_code="SUBMISSION_UNKNOWN",
                        phase="SUBMITTING",
                        retryable=False,
                        submission_unknown=True,
                        message=(
                            "submission unknown: venue lookup is not strongly consistent; "
                            "resubmission blocked"
                        ),
                    )
                # STRONG：venue 确认从未接受，释放幂等键后按新单继续走完整门禁
                storage.mark_idempotency_failed(idempotency_key)
            else:
                # 查询结果异常（无 order_id 的记录）：保持占用，禁止重提
                raise structured_error(
                    409,
                    error_code="SUBMISSION_UNKNOWN",
                    phase="SUBMITTING",
                    retryable=False,
                    submission_unknown=True,
                    message=(
                        "submission unknown: idempotency key occupied without "
                        "order_id; venue lookup inconclusive; resubmission blocked"
                    ),
                )'''
assert old_block in src, "anchor 2 not found"
src = src.replace(old_block, new_block)
io.open(p, "w", encoding="utf-8", newline="\n").write(src)
print("orders.py: FAILED fast-path added")

p = "services/quant-gateway/tests/test_orders_api.py"
src = io.open(p, encoding="utf-8").read()
old = '''    with gw_storage.locked_conn() as conn:
        rows = conn.execute(
            "SELECT action, market, subject_id, outcome, detail FROM audit_log "
            "WHERE action = ? ORDER BY occurred_at", (action,)
        ).fetchall()
    return [dict(r) for r in rows]'''
new = '''    keys = ("action", "market", "subject_id", "outcome", "detail")
    with gw_storage.locked_conn() as conn:
        rows = conn.execute(
            "SELECT action, market, subject_id, outcome, detail FROM audit_log "
            "WHERE action = ? ORDER BY occurred_at", (action,)
        ).fetchall()
    return [dict(zip(keys, r)) for r in rows]'''
assert old in src, "test anchor not found"
io.open(p, "w", encoding="utf-8", newline="\n").write(src.replace(old, new))
print("test parse fixed")
