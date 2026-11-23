from sqlalchemy import or_
from app.extensions import db

from app.generated.models.finished_goods_batches import FinishedGoodsBatch
from app.generated.models.process_orders import ProcessOrder

class BatchNoDuplicateError(ValueError):
    """生产批号重复异常。"""
    pass


class BatchValidationService:

    @staticmethod
    def ensure_batch_no_unique(batch_no, exclude_order_id=None):
        """检查成品批次档案，避免与工艺单入库或手动录入的成品批号重复。

        编辑工艺单时，允许保留当前工艺单自己已经生成的成品批号；
        但其他工艺单或手动录入的同批号仍然视为重复。
        """
        value = str(batch_no or "").strip()
        if not value:
            raise BatchNoDuplicateError("生产批号必填")

        query = FinishedGoodsBatch.query.filter(
            FinishedGoodsBatch.batch_no == value
        )
        if exclude_order_id is not None:
            # 只排除当前工艺单自身生成的批次；process_order_id 为 NULL
            # 的手工录入批次仍然必须参与重复检查。
            query = query.filter(
                or_(
                    FinishedGoodsBatch.process_order_id.is_(None),
                    FinishedGoodsBatch.process_order_id != int(exclude_order_id),
                )
            )

        existing = query.first()
        if existing:
            if existing.process_order_id:
                source = f"来源工艺单 #{existing.process_order_id}"
                order = db.session.get(ProcessOrder, existing.process_order_id)
                if order:
                    source += f", {order.process_order_no}"
            else:
                source = "手动录入或非工艺单来源"
            raise BatchNoDuplicateError(
                f"生产批号“{value}”已存在于成品批次档案中（{source}），"
                "请更换生产批号后再创建。"
            )
        return value