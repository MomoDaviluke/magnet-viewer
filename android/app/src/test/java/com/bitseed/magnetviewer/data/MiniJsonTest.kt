package com.bitseed.magnetviewer.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 极简 JSON 编解码单测（JVM）：解析必须**对任何坏输入返回 null**（清单容错的第一道防线），
 * 编码必须确定性（键排序 + 缩进）且可被自己解析回来。
 */
class MiniJsonTest {

  @Test
  fun numbers_integralAndLargeValuesStayPlain() {
    assertEquals(161664, 161664.0.toLong().toInt())
    assertEquals("161664", numToString(161664.0))
    assertEquals("-12.5", numToString(-12.5))
    assertEquals("1789383223.16", numToString(1789383223.16))
    assertEquals("1789383223", numToString(1789383223.0))
    assertEquals("0", numToString(Double.NaN))
    assertEquals("0", numToString(Double.POSITIVE_INFINITY))
  }

  @Test
  fun roundTrip_objectWithAllTypes() {
    val v =
      Jv.Obj(
        linkedMapOf(
          "s" to Jv.Str("中文 \"带引号\" \\ 反斜杠\n换行"),
          "n" to Jv.Num(-12.5),
          "i" to Jv.Num(161664.0),
          "b" to Jv.Bool(true),
          "nil" to Jv.Null,
          "arr" to Jv.Arr(listOf(Jv.Num(1.0), Jv.Str("二"), Jv.Obj(linkedMapOf("k" to Jv.Bool(false))))),
          "emptyArr" to Jv.Arr(emptyList()),
          "emptyObj" to Jv.Obj(emptyMap()),
        )
      )
    val text = v.encode()
    val back = parseJv(text)
    assertEquals(v, back)
    assertTrue("整数值不带小数点", text.contains("\"i\": 161664"))
    assertTrue("非整数值保留小数", text.contains("\"n\": -12.5"))
  }

  @Test
  fun encode_isSortedAndIndented() {
    val v = Jv.Obj(linkedMapOf("z" to Jv.Num(1.0), "a" to Jv.Num(2.0), "m" to Jv.Num(3.0)))
    val text = v.encode()
    // 与桌面 json.dumps(indent=2, sort_keys=True) 同形：键排序 + 「: 」分隔
    assertEquals("{\n  \"a\": 2,\n  \"m\": 3,\n  \"z\": 1\n}", text)
    // 非排序模式保留插入顺序
    assertEquals("{\n  \"z\": 1,\n  \"a\": 2,\n  \"m\": 3\n}", v.encode(sortKeys = false))
    assertEquals("{\"a\": 2,\"m\": 3,\"z\": 1}", v.encode(pretty = false))
    assertEquals("单行模式也和 json.dumps 一致（逗号后有空格）", "{\"a\": 2}", Jv.Obj(linkedMapOf("a" to Jv.Num(2.0))).encode(pretty = false))
  }

  @Test
  fun parser_rejectsMalformedInput() {
    val bad =
      listOf(
        null,
        "",
        "   ",
        "{",
        "}",
        "[1,2",
        "{\"a\":}",
        "{\"a\" 1}",
        "{\"a\":1,}",
        "tru",
        "\"未闭合",
        "nul",
        "1 2",
        "{\"a\":1} 多余",
        "{'a':1}",
        "{\"a\":\"\\uZZZZ\"}",
      )
    for (t in bad) {
      assertNull("坏输入必须返回 null：${t?.take(20)}", parseJv(t))
    }
  }

  @Test
  fun parser_rejectsTooDeepNesting() {
    val deep = "[".repeat(JV_MAX_DEPTH + 5) + "]".repeat(JV_MAX_DEPTH + 5)
    assertNull("超过深度上限必须判非法（防炸栈）", parseJv(deep))
    val ok = "[".repeat(8) + "]".repeat(8)
    assertTrue(parseJv(ok) != null)
  }

  @Test
  fun accessors_areTolerant() {
    val v = parseJv("""{"s":"x","n":"42","i":7,"f":1.5,"b":"yes","bad":true,"arr":[1,2]}""")!!
    assertEquals("x", v.strOr("s"))
    assertEquals("缺键取默认", "d", v.strOr("nope", "d"))
    assertEquals("数字字符串也能取整数", 42, v.intOr("n"))
    assertEquals("非数字字符串取默认", 0, v.intOr("s"))
    assertEquals(7, v.intOr("i"))
    assertEquals(1.5, v.doubleOrNull("f")!!, 0.0)
    assertNull("布尔不该被当数字", v.doubleOrNull("b"))
    assertTrue(v.boolOr("b"))
    assertEquals(2, v.listOrEmpty("arr").size)
    assertEquals(0, v.listOrEmpty("s").size)
    assertEquals("数字字段取字符串", "7", v.strOr("i"))
  }

  @Test
  fun truncation_detected() {
    val text = encodeTasks(
      mapOf(
        "444b8b5c9a69b388a5013d85a42caef060f03225" to
          TaskRecord(infoHash = "444b8b5c9a69b388a5013d85a42caef060f03225", name = "fixture")
      )
    )
    assertTrue("完整文本可解析", parseJv(text) != null)
    for (cut in listOf(1, 5, text.length / 3, text.length - 1)) {
      assertNull("截断（前 $cut 字节）必须判非法", parseJv(text.substring(0, cut)))
    }
  }
}
