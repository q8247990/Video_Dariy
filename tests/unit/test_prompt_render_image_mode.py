"""图像帧模式（image mode）Prompt 渲染测试（todo 22）。

video 模式快照用于锁定识别 prompt 的渲染结果，模板每次有意修订都要同步更新快照；
image 模式新增帧时间戳列表、短事件规则 5/6 与 17 事件软上限。
"""

import base64
from datetime import datetime

import pytest

from src.application.prompt.compiler import compile_video_recognition_prompt
from src.application.prompt.contracts import (
    SessionPromptContext,
    StrategyPromptContext,
    VideoRecognitionPromptInput,
    VideoSourcePromptContext,
)
from src.services.prompt_builder.v2.video_recognition import build_video_recognition_prompt
from src.services.video_analysis.enums import VIDEO_EVENT_TYPES

# 当前模板渲染出的 video 模式 system prompt 全文（识别 prompt 修订后重新生成；原文含
# 超过 100 字符的行，而仓库 ruff line-length 为 100，故以 base64 字面量保存，解码后逐字节
# 还原）。
VIDEO_MODE_SNAPSHOT = base64.b64decode(
    "5L2g5piv5a625bqt55uR5o6n6KeG6aKR6K+G5Yir5Yqp5omL77yM55uu5qCH5piv5o+Q5Y+W57uT"
    "5p6E5YyW5LqL5Lu25bm26L6T5Ye65Y+v6Kej5p6QIEpTT07jgIIKCuinhOWIme+8mgoxLiDkuovl"
    "rp7kvJjlhYjvvJrlhYjmj4/ov7Dlj6/op4Llr5/ooYzkuLrvvIzlho3lgZrovbvluqbmpoLmi6zj"
    "gIIKMi4g5L+d5a6I6K+G5Yir77ya6Lqr5Lu95LiN56Gu5a6a5pe25L2/55SoIHN1c3BlY3RlZCDm"
    "iJYgdW5rbm93bu+8jOemgeatouiZmuaehOWQjeWtl+OAggozLiDlkb3lkI3kvJjlhYjvvJrlvZPo"
    "r4bliKvnirbmgIHkuLogY29uZmlybWVkIOS4lOWPr+WMuemFjeWutuW6reaho+ahiOaXtu+8jHN1"
    "bW1hcnlfdGV4dCDkuI7kuovku7bmlofmnKzkvJjlhYjkvb/nlKjmoaPmoYjlp5PlkI3vvIjmiJDl"
    "kZjkuI7lrqDnianpg73pgILnlKjvvInjgIIKNC4g54q25oCB6L6555WM77ya56aB5q2i6L6T5Ye6"
    "5byA5b+DL+eUn+eXhS/mipHpg4HnrYnkuLvop4LmiJbljLvlrablv4PnkIbliKTmlq3jgIIKNS4g"
    "5LqL5Lu257KS5bqm77ya6L+e57ut5ZCM57G75rS75Yqo5bC96YeP5ZCI5bm277yM5LuF5Zyo5piO"
    "5pi+5Y+Y5YyW5pe25ouG5YiG44CCCjYuIOaXtumXtOinhOWIme+8mm9mZnNldF9zdGFydF9zZWMg"
    "LyBvZmZzZXRfZW5kX3NlYyDku6XlvZPliY3ovpPlhaXop4bpopHniYfmrrXotbfngrnkuLogMCDn"
    "p5LvvIzkuI3mmK8gc2Vzc2lvbiDotbfngrnjgIIKNy4g6aOO5qC86L6555WM77ya6aOO5qC85Y+q"
    "5b2x5ZON6Ieq54S26K+t6KiA5a2X5q615o6q6L6e77yM5LiN5pS55Y+Y6K+G5Yir6L6555WM44CC"
    "CjguIOi+k+WHuue6puadn++8muS7hei+k+WHuiBKU09OIOWvueixoe+8jOS4jeimgei+k+WHuumi"
    "neWkluaWh+Wtl+OAggo5LiDlubLmibDmjpLpmaTvvJrms6jmhI/nlLXop4blj43lhYnjgIHnjrvn"
    "koPlj43lsITjgIHotbDlu4rov5zlpITlsI/nm67moIfnrYnor6/liKTpo47pmanjgIIKMTAuIOiv"
    "reiogOe7n+S4gO+8muaJgOacieiHqueEtuivreiogOWtl+aute+8iHRpdGxl44CBc3VtbWFyeeOA"
    "gWRldGFpbOOAgXN1bW1hcnlfdGV4dOOAgW1haW5fc3ViamVjdHPjgIFvYnNlcnZlZF9hY3Rpb25z"
    "44CBaW50ZXJwcmV0ZWRfc3RhdGXjgIFkaXNwbGF5X25hbWXjgIFhbmFseXNpc19ub3Rlcy5ub3Rl"
    "77yJ5LiA5b6L5L2/55So566A5L2T5Lit5paH77yb56aB5q2i6Iux5paH5Y2V6K+N44CB6Iux5paH"
    "5p6a5Li+5ZCN5LiO5ou86Z+z44CCCjExLiDlj5blgLzop4TojIPvvJpvYnNlcnZlZF9hY3Rpb25z"
    "IOWPquWhq+WPr+inguWvn+eahOS4reaWh+WKqOS9nOefreivje+8iOWmguKAnOermeeri+KAneKA"
    "nOi1sOWKqOKAneKAnOWXheaOouKAne+8ie+8jGludGVycHJldGVkX3N0YXRlIOWPquWhq+WPr+in"
    "guWvn+eahOS4reaWh+eKtuaAgeefreivje+8iOWmguKAnOWcqOWcuuKAneKAnOitpuinieKAneKA"
    "nOmdoOi/keKAne+8ie+8jOS6jOiAheS4jeW+l+i+k+WHuuiLseaWhyB0b2tlbuOAggoxMi4g5ZCN"
    "56ew6KeE6IyD77yaZGlzcGxheV9uYW1lIOW/hemhu+aYr+WPr+ivu+S4reaWh+ensOiwk++8iOaI"
    "kOWRmOeUqOaho+ahiOWQjeaIluKAnOeIuOeIuC/lpojlpojigJ3nrYnop5LoibLlkI3vvIzlrqDn"
    "iannlKjmoaPmoYjlkI3miJbigJznjKsv54uX4oCd562J77yM6ZmM55Sf5Lq655So4oCc6ZmM55Sf"
    "5Lq64oCd4oCc5pyq6K+G5Yir5Lq65ZGY4oCd77yJ77yM56aB5q2i6L6T5Ye6IHVua25vd25fcGVy"
    "c29u44CBdW5rbm93bl9wZXTjgIF1bmtub3du44CBc3VzcGVjdGVkX2NhdCDnrYnoi7HmlofljaDk"
    "vY3nrKbvvIzkuZ/kuI3opoHmt7vliqDigJznlpHkvLzigJ3liY3nvIDjgIIKMTMuIOaho+ahiOWM"
    "uemFje+8mm1hdGNoZWRfcHJvZmlsZV9uYW1lIOS7heW9k+WvueixoeS4juWutuW6reaho+ahiOS4"
    "reafkOS4quWnk+WQjeS4gOiHtOaXtuWhq+WGme+8jOS4lOW/hemhu+S4juaho+ahiOS4reaWh+Wn"
    "k+WQjemAkOWtl+ebuOWQjO+8m+emgeatouaLvOmfs+OAgeiLseaWh+e/u+ivkeaIluWIq+WQje+8"
    "iOWmgiBGZWlmZWnjgIFNb23jgIFMaXpp77yJ44CC5peg5rOV6YCQ5a2X5Yy56YWN5pe25aGrIG51"
    "bGzjgIIKMTQuIOWFs+azqOmhueaJk+agh++8muiLpeS6i+S7tuWRveS4reWutuW6reWFs+azqOmh"
    "ue+8jOWwhuWFtiBrZXkg5aGr5YWlIGZvY3VzX21hdGNoZXPvvJvlj6rog73kvb/nlKjlrrbluq3k"
    "uIrkuIvmlofliJflh7rnmoQga2V577yM5peg5Yy56YWN5pe25aGrIFtd77yM56aB5q2i6Ieq6YCg"
    "IGtleeOAggoxNS4g5Y+Y5YyW5Yik5a6a77ya5q+U6L6D55u46YK75binL+i/nue7reeUu+mdouS4"
    "reS4u+S9k+eahOS9jee9ruOAgeWnv+aAgeOAgeaVsOmHj+S4jui/m+WHuu+8m+WPquacieWHuueO"
    "sOWPr+inguWvn+WPmOWMluaXtuaJjeeUn+aIkOS6i+S7tuOAguiLpeS4u+S9k+WcqOacrOauteWG"
    "hemmluasoeWHuueOsC/nprvlvIDvvIzmiJblp7/mgIHlj5HnlJ/mmI7mmL7lj5jljJbvvIjlpoLn"
    "q5nnq4vihpLotrTljafvvInvvIzku43lupTorrDlvZXlr7nlupTkuovku7bvvJvmlbTmrrXml6Dk"
    "u7vkvZXlj6/op4Llr5/lj5jljJbml7bvvIxldmVudHMg5b+F6aG75Li656m65pWw57uE77yM5bm2"
    "5ZyoIGFuYWx5c2lzX25vdGVzIOS4reS7pSBub19zaWduaWZpY2FudF9ldmVudCDor7TmmI7vvIzn"
    "poHmraLkuLrkuobloavmu6HnpLrkvovnu5PmnoTogIznvJbpgKDkuovku7bjgIIKMTYuIOaRmOim"
    "geinhOiMg++8mnN1bW1hcnlfdGV4dCDlj6rlrqLop4LpmYjov7Dlj6/op4Llr5/kuovlrp7vvIjo"
    "sIHjgIHlnKjlk6rjgIHlgZrkuobku4DkuYjmiJblpITkuo7ku4DkuYjnirbmgIHvvInvvIzkuI3o"
    "poHlhpnmsJvlm7TjgIHmg4Xnu6rmiJblj5nkuovmgKfmj4/lhpnvvJvml6Dlrp7otKjlj5jljJbm"
    "l7blhpnigJznlLvpnaLml6DmmI7mmL7lj5jljJbigJ3jgIIKCui+k+WHuiBzY2hlbWHvvIjnpLrk"
    "vovku4XnpLrojIPlrZfmrrXmoLzlvI/kuI7kuK3mloflhpnms5XvvIzlrp7pmYXlhoXlrrnmjIno"
    "p4bpopHloavlhpnvvJvlrZfmrrXnu5PmnoTlv4XpobvkuKXmoLzpgbXlrojvvIk6CnsKICAic2Vz"
    "c2lvbl9zdW1tYXJ5IjogewogICAgInN1bW1hcnlfdGV4dCI6ICLlrqLljoXlhoXkuIDlkI3miJDl"
    "kZjlnKjljqjmiL/ljLrln5/mlbTnkIbnianlk4HvvIzpmo/lkI7nprvlvIDjgIIiLAogICAgImFj"
    "dGl2aXR5X2xldmVsIjogIm1lZGl1bSIsCiAgICAibWFpbl9zdWJqZWN0cyI6IFsi5aaI5aaIIl0K"
    "ICB9LAogICJldmVudHMiOiBbCiAgICB7CiAgICAgICJvZmZzZXRfc3RhcnRfc2VjIjogMCwKICAg"
    "ICAgIm9mZnNldF9lbmRfc2VjIjogMTIsCiAgICAgICJldmVudF90eXBlIjogIm1lbWJlcl9zdGF5"
    "IiwKICAgICAgInRpdGxlIjogIuWmiOWmiOWcqOWOqOaIv+WMuuWfn+aVtOeQhueJqeWTgSIsCiAg"
    "ICAgICJzdW1tYXJ5IjogIuWmiOWmiOWcqOeUu+mdouWPs+S+p+WOqOaIv+WMuuWfn+aVtOeQhueJ"
    "qeWTgeOAgiIsCiAgICAgICJkZXRhaWwiOiAi55S76Z2i5Y+z5L6n77yM5LiA5ZCN5aWz5oCn77yI"
    "5aaI5aaI77yJ56uZ5Zyo5Yaw566x5ZKM5qmx5p+c5YmN77yM5aSn6YOo5YiG5pe26Ze05L+d5oyB"
    "56uZ56uL77yM5YG25bCU5pyJ5omL6YOo5Yqo5L2c44CCIiwKICAgICAgInJlbGF0ZWRfZW50aXRp"
    "ZXMiOiBbCiAgICAgICAgewogICAgICAgICAgImVudGl0eV90eXBlIjogIm1lbWJlciIsCiAgICAg"
    "ICAgICAiZGlzcGxheV9uYW1lIjogIuWmiOWmiCIsCiAgICAgICAgICAibWF0Y2hlZF9wcm9maWxl"
    "X25hbWUiOiAi5aaI5aaIIiwKICAgICAgICAgICJyZWNvZ25pdGlvbl9zdGF0dXMiOiAiY29uZmly"
    "bWVkIiwKICAgICAgICAgICJjb25maWRlbmNlIjogMC45CiAgICAgICAgfQogICAgICBdLAogICAg"
    "ICAib2JzZXJ2ZWRfYWN0aW9ucyI6IFsi56uZ56uLIiwgIuaVtOeQhueJqeWTgSJdLAogICAgICAi"
    "aW50ZXJwcmV0ZWRfc3RhdGUiOiBbIuWcqOWcuiJdLAogICAgICAiZm9jdXNfbWF0Y2hlcyI6IFsi"
    "bWVtYmVyX2lub3V0Il0sCiAgICAgICJjb25maWRlbmNlIjogMC45CiAgICB9CiAgXSwKICAiYW5h"
    "bHlzaXNfbm90ZXMiOiBbeyJ0eXBlIjogImlkZW50aXR5X3VuY2VydGFpbiIsICJub3RlIjogIui3"
    "neemu+i+g+i/nO+8jOaXoOazleehruiupOi6q+S7veOAgiJ9XQp9Cgrnqbrnu5PmnpznpLrkvovv"
    "vIjmlbTmrrXml6Dlrp7otKjlj5jljJbml7bvvJtldmVudHMg5b+F6aG75Li656m677yM5LiN6KaB"
    "57yW6YCg5LqL5Lu277yJOgp7CiAgInNlc3Npb25fc3VtbWFyeSI6IHsKICAgICJzdW1tYXJ5X3Rl"
    "eHQiOiAi55S76Z2i5peg5piO5pi+5Y+Y5YyW44CCIiwKICAgICJhY3Rpdml0eV9sZXZlbCI6ICJs"
    "b3ciLAogICAgIm1haW5fc3ViamVjdHMiOiBbXQogIH0sCiAgImV2ZW50cyI6IFtdLAogICJhbmFs"
    "eXNpc19ub3RlcyI6IFt7InR5cGUiOiAibm9fc2lnbmlmaWNhbnRfZXZlbnQiLCAibm90ZSI6ICLn"
    "m7jpgrvluKfkuK3kuLvkvZPnmoTkvY3nva7jgIHlp7/mgIHkuI7mlbDph4/ml6DmmI7mmL7lj5jl"
    "jJbjgIIifV0KfQ=="
).decode("utf-8")

# 改动前模板渲染出的 video 模式 user prompt 全文（同样固定输入），逐字节相等。
USER_MODE_SNAPSHOT = base64.b64decode(
    "5a625bqt5LiK5LiL5paH77yaCuWutuW6reamguWGtTog5a625bqt5ZCNPeaIkeeahOWutuW6rTsg5qCH562+PWhh"
    "c19wZXQ7IOWFs+azqOmhuTog5pegOyDpo47moLw9ZmFtaWx5X2NvbXBhbmlvbjsg6aOO5qC85YGP5aW9PeeugOa0"
    "gTsg5Yqp5omL5ZCNPeWutuW6reWKqeaJizsg5aSH5rOoPeWknOmXtOi+g+WuiemdmS4K5a625bqt5oiQ5ZGYOiDm"
    "l6AuCuWuoOeJqeS/oeaBrzog5pegLgrovrnnlYw6IOWutuW6reaho+ahiOS7heS9nOS4uui+heWKqeS4iuS4i+aW"
    "h++8jOS4jeiDveabv+S7o+inhumikeivgeaNri4KCuaRhOWDj+WktOS4iuS4i+aWh++8mgrmkYTlg4/lpLQ957Gz"
    "5a62TkFTOyDlkI3np7A95a6i5Y6FOyDkvY3nva495a6i5Y6FOyDlpIfms6g955S16KeG5bGP5bmV5Lya5Y+N5YWJ"
    "LgoK5Lu75Yqh57qnIFByb21wdO+8mgrku7vliqHnm67moIc6IOWvueW9k+WJjSBzZXNzaW9uIOWBmue7k+aehOWM"
    "luivhuWIq++8jOi+k+WHuuWPr+WFpeW6k+S6i+S7tuOAggrkvJror53kuIrkuIvmloc6IHNlc3Npb25faWQ9MTAw"
    "OyBzb3VyY2VfaWQ9MTsgc3RhcnQ9MjAyNi0wMy0xM1QxMDowMDowMDsgZW5kPTIwMjYtMDMtMTNUMTA6MDE6MDA7"
    "IGR1cmF0aW9uPTYwOyBzZWdtZW50X2luZGV4PTA7IHNlZ21lbnRfc3RhcnRfb2Zmc2V0X3NlYz0wOyBzZWdtZW50"
    "X2R1cmF0aW9uX3NlY29uZHM9NjAuCuetlueVpeS4iuS4i+aWhzogaW5nZXN0X3R5cGU9eGlhb21pX25hc19iYWNr"
    "dXA7IHNvdXJjZV90eXBlPWxvY2FsX2RpcmVjdG9yeTsgc3RyYXRlZ3lfbm90ZT3kvJjlhYjlvZLnurMuCuS6i+S7"
    "tuaKveWPluinhOWImToKZXZlbnRfdHlwZSDlrprkuYnvvIjlv4Xpobvku47kuK3pgInmi6nvvIznpoHmraLoh6rp"
    "gKDmiJbovpPlh7roi7HmlofliKvlkI3vvIk6Ci0gbWVtYmVyX2FwcGVhcjog5a625bqt5oiQ5ZGY5Ye6546w5Zyo"
    "55S76Z2i5Lit77yI6aaW5qyh5Ye6546w5oiW55+t5pqC57uP6L+H77yM5peg5piO56Gu6L+b5Ye65Yqo5L2c77yJ"
    "Ci0gbWVtYmVyX2VudGVyOiDmiJDlkZjov5vlhaXnlLvpnaLmiJbmn5DkuKrljLrln5/vvIjmnInmmI7noa7nmoTo"
    "v5vlhaXliqjkvZzkuI7mlrnlkJHvvIkKLSBtZW1iZXJfbGVhdmU6IOaIkOWRmOemu+W8gOeUu+mdouaIluafkOS4"
    "quWMuuWfn++8iOacieaYjuehrueahOemu+W8gOWKqOS9nOS4juaWueWQke+8iQotIG1lbWJlcl9zdGF5OiDmiJDl"
    "kZjlnKjnlLvpnaLlhoXmjIHnu63lgZznlZnvvIjlnKjlnLrml7bpl7TovoPplb/kuJTml6DmmI7noa7ov5vlh7rl"
    "iqjkvZzvvIkKLSB1bmtub3duX3BlcnNvbl9hcHBlYXI6IOmZjOeUn+S6ui/mnKrnn6XkurrlkZjlh7rnjrDvvIjm"
    "l6Dms5XljLnphY3lrrbluq3moaPmoYjnmoTkurrlkZjvvIkKLSBwZXRfYXBwZWFyOiDlrqDnianlh7rnjrDlnKjn"
    "lLvpnaLkuK3vvIjpppbmrKHlh7rnjrDmiJbnn63mmoLnu4/ov4fvvIkKLSBwZXRfZW50ZXI6IOWuoOeJqei/m+WF"
    "peeUu+mdouaIluafkOS4quWMuuWfn++8iOacieaYjuehrueahOi/m+WFpeWKqOS9nO+8iQotIHBldF9sZWF2ZTog"
    "5a6g54mp56a75byA55S76Z2i5oiW5p+Q5Liq5Yy65Z+f77yI5pyJ5piO56Gu55qE56a75byA5Yqo5L2c77yJCi0g"
    "cGV0X3N0YXk6IOWuoOeJqeWcqOeUu+mdouWGheaMgee7reWBnOeVme+8iOaXoOaYjuaYvui/m+WHuuWKqOS9nOOA"
    "geS5n+WIpOWumuS4jeWHuua0u+WKqOaIluS8keaBr++8iQotIHBldF9hY3Rpdml0eTog5a6g54mp5rS75Yqo77yI"
    "6LWw5Yqo44CB5aWU6LeR44CB546p6ICN44CB5ZeF5o6i562J5piO56Gu5rS75Yqo77yJCi0gcGV0X3Jlc3Q6IOWu"
    "oOeJqeS8keaBr+aIlumdmeatou+8iOi2tOWNp+OAgeedoeinieOAgemVv+aXtumXtOS4jeWKqO+8iQotIG1lbWJl"
    "cl9wZXRfaW50ZXJhY3Rpb246IOaIkOWRmOS4juWuoOeJqeS6kuWKqAotIG11bHRpX2VudGl0eV9pbnRlcmFjdGlv"
    "bjog5aSa5Liq5a+56LGh5LmL6Ze05LqS5YqoCi0gYWJub3JtYWxfc3RheTog5byC5bi45YGc55WZ77yI6ZW/5pe2"
    "6Ze05LiN5Yqo44CB5byC5bi45L2N572u562J77yJCi0gc2NlbmVfYXR0ZW50aW9uX25lZWRlZDog5Zy65pmv6ZyA"
    "6KaB5YWz5rOo77yI5byC5bi45YWJ57q/44CB54mp5ZOB56e75Yqo562J77yJCi0gb3RoZXI6IOaXoOazleW9kuWF"
    "peS7peS4iuS7u+S9leWFt+S9k+exu+Wei+aXtuaJjeS9v+eUqO+8jOemgeatoueUqOWug+WFnOW6leW4uOingeWc"
    "uuaZrwrnsbvlnovovrnnlYzvvIjpgb/lhY3mt7fnlKjvvIk6Ci0gYXBwZWFyL2VudGVyL2xlYXZlIOS+p+mHjeKA"
    "nOWHuueOsC/ov5vlhaUv56a75byA55qE5Yqo5L2c5LiO5pa55ZCR4oCd77ybc3RheSDkvqfph43igJzlnKjlnLrl"
    "gZznlZnigJ3jgIIKLSBwZXRfYWN0aXZpdHkg5oyH5pyJ5piO56Gu5rS75Yqo77yI6LWw5YqoL+WllOi3kS/njqno"
    "gI0v5ZeF5o6i77yJ77ybcGV0X3Jlc3Qg5oyH5LyR5oGv5oiW6Z2Z5q2i77yI6La05Y2nL+edoeinie+8ie+8m+S7"
    "heWcqOWcuuS9huaXoOaYjuaYvua0u+WKqOaXtueUqCBwZXRfc3RheeOAggotIG1lbWJlcl9hcHBlYXIg55So5LqO"
    "6aaW5qyh5Ye6546w5oiW55+t5pqC57uP6L+H77yb5oyB57ut5Zyo5Zy65LiU5peg6L+b5Ye65Yqo5L2c55SoIG1l"
    "bWJlcl9zdGF544CCCi0g5LuF5b2T5peg5rOV5b2S5YWl5Lu75L2V5YW35L2T57G75Z6L5pe25omN55SoIG90aGVy"
    "77yM56aB5q2i55SoIG90aGVyIOWFnOW6leW4uOingeWcuuaZr+OAggphbmFseXNpc19ub3RlX3R5cGUg5p6a5Li+"
    "PWlkZW50aXR5X3VuY2VydGFpbuOAgWxvd192aXNpYmlsaXR544CBbm9fc2lnbmlmaWNhbnRfZXZlbnTjgIFvY2Ns"
    "dXNpb27jgIFwb3NzaWJsZV9mYWxzZV9wb3NpdGl2ZeOAgXNjZW5lX25vaXNlOyBtZXJnZV9zaW1pbGFyX2FjdGlv"
    "bnM9dHJ1ZTsgc3BsaXRfb25seV9vbl9zaWduaWZpY2FudF9jaGFuZ2U9dHJ1ZS4K5pyA57uI57qm5p2fOiDlj6ro"
    "g73ovpPlh7ogSlNPTiDlr7nosaE7IOaJgOaciSBvZmZzZXQg5b+F6aG75Li66Z2e6LSf5pWw5LiUIGVuZD49c3Rh"
    "cnQ7IG9mZnNldCDku6XlvZPliY3ovpPlhaXop4bpopHniYfmrrXotbfngrnkuLogMCDnp5I7IGV2ZW50X3R5cGUg"
    "5b+F6aG75p2l6Ieq5LiK6Z2i5a6a5LmJOyBkZXRhaWwg6ZyA6K+m57uG5o+P6L+w5Y+v6KeC5a+f5LqL5a6eOyDm"
    "iYDmnInoh6rnhLbor63oqIDlrZfmrrXkvb/nlKjnroDkvZPkuK3mlocu"
).decode("utf-8")

ORIGINAL_RULE_5 = "5. 事件粒度：连续同类活动尽量合并，仅在明显变化时拆分。"
ORIGINAL_RULE_6 = (
    "6. 时间规则：offset_start_sec / offset_end_sec "
    "以当前输入视频片段起点为 0 秒，不是 session 起点。"
)
IMAGE_RULE_5 = "5. 事件粒度：优先输出多个短事件，只有同一持续行为才合并。"
IMAGE_RULE_6 = (
    "6. 时间规则：offset_start_sec / offset_end_sec "
    "以本批第 0 帧为 0 秒，且必须对齐到给定帧时间戳。"
)
EVENT_CAP_SENTENCE = "每个文件最多输出 8 个事件"
CHANGE_RULE_SENTENCE = "15. 变化判定"
OBJECTIVE_SUMMARY_RULE_SENTENCE = "16. 摘要规范"
EMPTY_RESULT_MARKER = "空结果示例"
FRAME_LIST_HEADER = "本批帧时间戳："
VIDEO_OFFSET_CLAUSE = "offset 以当前输入视频片段起点为 0 秒"
IMAGE_OFFSET_CLAUSE = "offset 以本批第 0 帧为 0 秒"


def _base_input() -> dict:
    """与 tests/unit/test_video_recognition_prompt_builder.py 相同的固定输入。"""
    return {
        "home_context": {
            "home_profile": {
                "home_name": "我的家庭",
                "family_tags": ["has_pet"],
                "focus_points": ["pet_status"],
                "system_style": "family_companion",
                "style_preference_text": "简洁",
                "assistant_name": "家庭助手",
                "home_note": "夜间较安静",
            },
            "members": [],
            "pets": [],
        },
        "video_source": {
            "source_name": "米家NAS",
            "camera_name": "客厅",
            "location_name": "客厅",
            "prompt_text": "电视屏幕会反光",
            "source_type": "local_directory",
        },
        "session_context": {
            "session_id": 100,
            "source_id": 1,
            "session_start_time": datetime(2026, 3, 13, 10, 0, 0),
            "session_end_time": datetime(2026, 3, 13, 10, 1, 0),
            "total_duration_seconds": 60,
            "segment_index": 0,
            "segment_start_offset_sec": 0,
            "segment_duration_seconds": 60,
        },
        "strategy_context": {
            "ingest_type": "xiaomi_nas_backup",
            "source_type": "local_directory",
            "strategy_note": "优先归纳",
        },
    }


def _frame_lines(user_prompt: str) -> list[str]:
    return [line for line in user_prompt.splitlines() if line.startswith("帧 ")]


def test_video_mode_system_prompt_matches_snapshot() -> None:
    system_prompt, _ = build_video_recognition_prompt(_base_input())

    assert system_prompt == VIDEO_MODE_SNAPSHOT


def test_video_mode_user_prompt_matches_pre_change_snapshot() -> None:
    _, user_prompt = build_video_recognition_prompt(_base_input())

    assert user_prompt == USER_MODE_SNAPSHOT


def test_video_mode_keeps_original_rule5_rule6_and_has_no_frame_header() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(_base_input())

    assert ORIGINAL_RULE_5 in system_prompt
    assert ORIGINAL_RULE_6 in system_prompt
    assert FRAME_LIST_HEADER not in user_prompt
    # 最终约束中的 offset 子句保持 video 版，不出现 image 版。
    assert VIDEO_OFFSET_CLAUSE in user_prompt
    assert IMAGE_OFFSET_CLAUSE not in user_prompt


def test_video_mode_empty_timestamps_is_valid_and_matches_snapshot() -> None:
    # video 模式 + 空 frame_timestamps 必须合法：不抛错，且不渲染帧区段，
    # 渲染结果与改动前快照逐字节一致。
    system_prompt, user_prompt = build_video_recognition_prompt(
        {**_base_input(), "frame_timestamps": []}
    )

    assert system_prompt == VIDEO_MODE_SNAPSHOT
    assert user_prompt == USER_MODE_SNAPSHOT


def test_image_mode_renders_three_frame_lines_in_order() -> None:
    system_prompt, user_prompt = build_video_recognition_prompt(
        {
            **_base_input(),
            "input_mode": "image",
            "frame_timestamps": [0.0, 3.5, 7.0],
        }
    )

    expected_section = f"{FRAME_LIST_HEADER}\n帧 1/3 — t=0.0s\n帧 2/3 — t=3.5s\n帧 3/3 — t=7.0s"
    assert expected_section in user_prompt
    assert user_prompt.count(FRAME_LIST_HEADER) == 1
    assert _frame_lines(user_prompt) == ["帧 1/3 — t=0.0s", "帧 2/3 — t=3.5s", "帧 3/3 — t=7.0s"]

    # image 模式下规则 5/6 换成图像版，且包含 17 事件软上限；video 版规则不再出现。
    assert IMAGE_RULE_5 in system_prompt
    assert IMAGE_RULE_6 in system_prompt
    assert ORIGINAL_RULE_5 not in system_prompt
    assert ORIGINAL_RULE_6 not in system_prompt
    assert "17. 事件数量上限" in system_prompt
    assert EVENT_CAP_SENTENCE in system_prompt
    # 最终约束中的 offset 子句换成 image 版，与系统提示规则 6 不再冲突。
    assert IMAGE_OFFSET_CLAUSE in user_prompt
    assert VIDEO_OFFSET_CLAUSE not in user_prompt

    # 16 个事件类型名必须全部保留。
    for event_type in sorted(VIDEO_EVENT_TYPES):
        assert event_type in user_prompt


def test_change_detection_and_empty_result_guidance_are_rendered() -> None:
    # 静止误报的核心 prompt 修复：显式允许空 events，并要求先做帧间变化判定。
    for input_mode in ("video", "image"):
        overrides: dict = {"input_mode": input_mode}
        if input_mode == "image":
            overrides["frame_timestamps"] = [0.0, 3.5, 7.0]
        system_prompt, _ = build_video_recognition_prompt({**_base_input(), **overrides})

        assert CHANGE_RULE_SENTENCE in system_prompt
        assert OBJECTIVE_SUMMARY_RULE_SENTENCE in system_prompt
        assert EMPTY_RESULT_MARKER in system_prompt
        assert '"events": []' in system_prompt
        assert "no_significant_event" in system_prompt
        # 旧的静止偏置必须消失：不再把“静止”作为状态示例，也不再要求忽略静止。
        assert "（如“静止”" not in system_prompt
        assert "不要因短暂静止忽略整段价值" not in system_prompt


def test_image_mode_single_frame() -> None:
    _, user_prompt = build_video_recognition_prompt(
        {
            **_base_input(),
            "input_mode": "image",
            "frame_timestamps": [12.5],
        }
    )

    assert f"{FRAME_LIST_HEADER}\n帧 1/1 — t=12.5s" in user_prompt
    assert _frame_lines(user_prompt) == ["帧 1/1 — t=12.5s"]


def test_image_mode_empty_timestamps_raises() -> None:
    # image 模式空 frame_timestamps 属于调用方错误，必须在渲染前响亮失败，
    # 而不是静默渲染出一个只有表头的空区段；错误信息必须点名缺失的输入。
    with pytest.raises(ValueError, match="frame timestamp"):
        build_video_recognition_prompt(
            {
                **_base_input(),
                "input_mode": "image",
                "frame_timestamps": [],
            }
        )


def test_image_mode_non_monotonic_timestamps_raise() -> None:
    # 时间戳乱序属于调用方错误，必须抛出 ValueError 并指明出错的下标。
    with pytest.raises(ValueError) as excinfo:
        build_video_recognition_prompt(
            {
                **_base_input(),
                "input_mode": "image",
                "frame_timestamps": [1.0, 0.5],
            }
        )
    assert "index 1" in str(excinfo.value)


def _compiler_input(**overrides: object) -> VideoRecognitionPromptInput:
    base = {
        "home_context": _base_input()["home_context"],
        "video_source": VideoSourcePromptContext(
            source_name="米家NAS",
            camera_name="客厅",
            location_name="客厅",
            prompt_text="电视屏幕会反光",
            source_type="local_directory",
        ),
        "session_context": SessionPromptContext(
            session_id=100,
            source_id=1,
            session_start_time=datetime(2026, 3, 13, 10, 0, 0),
            session_end_time=datetime(2026, 3, 13, 10, 1, 0),
            total_duration_seconds=60,
        ),
        "strategy_context": StrategyPromptContext(
            ingest_type="xiaomi_nas_backup",
            source_type="local_directory",
            strategy_note="优先归纳",
        ),
        "event_type_list": sorted(VIDEO_EVENT_TYPES),
    }
    base.update(overrides)
    return VideoRecognitionPromptInput(**base)


def test_compiler_defaults_preserve_video_mode() -> None:
    system_prompt, user_prompt = compile_video_recognition_prompt(_compiler_input())

    assert system_prompt == VIDEO_MODE_SNAPSHOT
    assert FRAME_LIST_HEADER not in user_prompt


def test_compiler_image_mode_passes_frame_list() -> None:
    system_prompt, user_prompt = compile_video_recognition_prompt(
        _compiler_input(input_mode="image", frame_timestamps=[0.0, 3.5, 7.0])
    )

    assert IMAGE_RULE_5 in system_prompt
    assert EVENT_CAP_SENTENCE in system_prompt
    assert _frame_lines(user_prompt) == ["帧 1/3 — t=0.0s", "帧 2/3 — t=3.5s", "帧 3/3 — t=7.0s"]


def test_compiler_image_mode_empty_timestamps_raises() -> None:
    # 编译器路径（dataclass 构造 + compiler）在 image 模式空列表时同样必须响亮失败，
    # 保证编译器校验无法被绕过。
    with pytest.raises(ValueError, match="frame timestamp"):
        compile_video_recognition_prompt(_compiler_input(input_mode="image", frame_timestamps=[]))
