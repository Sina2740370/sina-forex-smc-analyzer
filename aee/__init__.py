"""AEE — موتور شواهد تطبیقی (فاز ۰: چارچوب آمار و پایپ‌لاین ویژگی).

قاعدهٔ معماری: امتیازدهی به‌جای ردکردن؛ هر ابزار اینجا فقط شواهد می‌سازد،
فیلتر نهایی در لایهٔ ریسک است.
"""
from aee.stats import benjamini_hochberg, bootstrap_mean_ci, event_study_summary
from aee.cv import PurgedWalkForward

__all__ = [
    "benjamini_hochberg",
    "bootstrap_mean_ci",
    "event_study_summary",
    "PurgedWalkForward",
]
