package com.bitseed.magnetviewer.data

/**
 * 极简 JSON 值模型 + 编解码（**纯 Kotlin，零第三方依赖**）。
 *
 * 为什么自己写：运行时 classpath 上没有 kotlinx-serialization-json（只有 JVM 单测源集有），
 * 而 `org.json` 在 JVM 单测里不可用（android.jar 的桩方法会抛 "not mocked"，除非打开
 * `isReturnDefaultValues`）。本文件只服务 `.tasks.json` 这一种载荷，够用即可：
 *
 * - [encode]：确定性输出（键排序 + 2 空格缩进 + 非 ASCII 直出），
 *   与桌面 `json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)` 同形；
 * - [parseJv]：任何非法输入返回 null（**绝不抛**），带嵌套深度上限（防恶意深度炸栈）。
 *
 * 目录内所有方法都是纯函数，可直接在 JVM 单测里跑。
 */
sealed interface Jv {
  object Null : Jv

  data class Bool(val value: Boolean) : Jv

  data class Num(val value: Double) : Jv

  data class Str(val value: String) : Jv

  data class Arr(val items: List<Jv>) : Jv

  data class Obj(val fields: Map<String, Jv>) : Jv
}

/** JSON 解析的最大嵌套深度（超过即视为非法：损坏文件不该把解析器炸栈）。 */
const val JV_MAX_DEPTH: Int = 64

// ---------------------------------------------------------------- 读取便捷方法（全部容错）

fun Jv?.asObj(): Jv.Obj? = this as? Jv.Obj

fun Jv?.asArr(): Jv.Arr? = this as? Jv.Arr

/** 取对象字段（非对象 / 缺键 → null）。 */
fun Jv?.field(key: String): Jv? = (this as? Jv.Obj)?.fields?.get(key)

/** 字符串字段：字符串原样，数字/布尔按字面量转字符串，其余取 [default]。 */
fun Jv?.strOr(key: String, default: String = ""): String =
  when (val v = field(key)) {
    is Jv.Str -> v.value
    is Jv.Num -> numToString(v.value)
    is Jv.Bool -> v.value.toString()
    else -> default
}

fun Jv?.intOr(key: String, default: Int = 0): Int {
  val v = field(key) ?: return default
  return when (v) {
    is Jv.Num -> v.value.toInt()
    is Jv.Str -> v.value.trim().toDoubleOrNull()?.toInt() ?: default
    is Jv.Bool -> if (v.value) 1 else 0
    else -> default
  }
}

fun Jv?.longOr(key: String, default: Long = 0L): Long {
  val v = field(key) ?: return default
  return when (v) {
    is Jv.Num -> v.value.toLong()
    is Jv.Str -> v.value.trim().toDoubleOrNull()?.toLong() ?: default
    is Jv.Bool -> if (v.value) 1L else 0L
    else -> default
  }
}

/** 浮点字段：`null` / 布尔 / 非法值 → null（桌面 `_float` 同款容忍：不把 null 当 0）。 */
fun Jv?.doubleOrNull(key: String): Double? =
  when (val v = field(key)) {
    is Jv.Num -> v.value
    is Jv.Str -> v.value.trim().toDoubleOrNull()
    else -> null
  }

/** 布尔字段：兼容 `true / 1 / "true" / "1" / "yes"`（桌面 `normalize_task` 的 seed 判据同款）。 */
fun Jv?.boolOr(key: String, default: Boolean = false): Boolean =
  when (val v = field(key)) {
    is Jv.Bool -> v.value
    is Jv.Num -> v.value != 0.0
    is Jv.Str ->
      when (v.value.trim().lowercase()) {
        "true",
        "1",
        "yes" -> true
        "false",
        "0",
        "no" -> false
        else -> default
      }
    else -> default
  }

/** 数组字段（非数组 → 空表）。 */
fun Jv?.listOrEmpty(key: String): List<Jv> = (field(key) as? Jv.Arr)?.items ?: emptyList()

// ---------------------------------------------------------------- 写出

/** 数字字面量：整数值不带小数点；大数不用科学计数法（与 Python `json.dumps` 的输出习惯一致）。 */
fun numToString(d: Double): String {
  if (d.isNaN() || d.isInfinite()) return "0"
  val asLong = d.toLong()
  if (asLong.toDouble() == d && kotlin.math.abs(d) < 1e15) return asLong.toString()
  val s = d.toString()
  // Kotlin 的 Double.toString 对大数会输出 1.78938322316E9 这种科学计数法：
  // JSON 虽合法，但与桌面 sort_keys/indent 风格的文本不一致，且肉眼难读 → 展开成普通写法
  return if (s.contains('E') || s.contains('e')) java.math.BigDecimal(s).toPlainString() else s
}

/**
 * 编码为 JSON 文本。[pretty] = 2 空格缩进；[sortKeys] = 键名排序
 * （桌面 `sort_keys=True`：同一份清单在任何机器/任何次运行都逐字节一致，便于对拍）。
 */
fun Jv.encode(pretty: Boolean = true, sortKeys: Boolean = true): String {
  val sb = StringBuilder(256)
  writeJv(sb, this, pretty, sortKeys, 0)
  return sb.toString()
}

private fun writeJv(sb: StringBuilder, v: Jv, pretty: Boolean, sortKeys: Boolean, depth: Int) {
  when (v) {
    Jv.Null -> sb.append("null")
    is Jv.Bool -> sb.append(if (v.value) "true" else "false")
    is Jv.Num -> sb.append(numToString(v.value))
    is Jv.Str -> writeJvString(sb, v.value)
    is Jv.Arr -> {
      if (v.items.isEmpty()) {
        sb.append("[]")
        return
      }
      sb.append('[')
      v.items.forEachIndexed { idx, item ->
        if (idx > 0) sb.append(',')
        newline(sb, pretty, depth + 1)
        writeJv(sb, item, pretty, sortKeys, depth + 1)
      }
      newline(sb, pretty, depth)
      sb.append(']')
    }
    is Jv.Obj -> {
      if (v.fields.isEmpty()) {
        sb.append("{}")
        return
      }
      val keys = if (sortKeys) v.fields.keys.sorted() else v.fields.keys.toList()
      sb.append('{')
      keys.forEachIndexed { idx, k ->
        if (idx > 0) sb.append(',')
        newline(sb, pretty, depth + 1)
        writeJvString(sb, k)
        sb.append(": ")
        v.fields[k]?.let { writeJv(sb, it, pretty, sortKeys, depth + 1) }
      }
      newline(sb, pretty, depth)
      sb.append('}')
    }
  }
}

private fun newline(sb: StringBuilder, pretty: Boolean, depth: Int) {
  if (!pretty) return
  sb.append('\n')
  repeat(depth) { sb.append("  ") }
}

private fun writeJvString(sb: StringBuilder, s: String) {
  sb.append('"')
  for (ch in s) {
    when (ch) {
      '"' -> sb.append("\\\"")
      '\\' -> sb.append("\\\\")
      '\n' -> sb.append("\\n")
      '\r' -> sb.append("\\r")
      '\t' -> sb.append("\\t")
      '\b' -> sb.append("\\b")
      '\u000C' -> sb.append("\\f")
      else -> if (ch < ' ') sb.append(String.format("\\u%04x", ch.code)) else sb.append(ch)
    }
  }
  sb.append('"')
}

// ---------------------------------------------------------------- 解析（失败一律 null）

/** 解析 JSON；任何非法输入（含 [text] 为 null、空串、截断、类型不符）返回 null。 */
fun parseJv(text: String?): Jv? {
  if (text == null) return null
  return try {
    val p = JvParser(text)
    val v = p.parseValue(0)
    p.skipWs()
    if (!p.eof()) null else v
  } catch (_: Throwable) {
    null
  }
}

private class JvParser(private val s: String) {
  private var i = 0

  fun eof(): Boolean = i >= s.length

  fun skipWs() {
    while (i < s.length && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++
  }

  fun parseValue(depth: Int): Jv {
    if (depth > JV_MAX_DEPTH) throw IllegalArgumentException("JSON 嵌套过深")
    skipWs()
    if (eof()) throw IllegalArgumentException("JSON 意外结束")
    return when (val c = s[i]) {
      '{' -> parseObj(depth)
      '[' -> parseArr(depth)
      '"' -> Jv.Str(parseString())
      't' -> literal("true", Jv.Bool(true))
      'f' -> literal("false", Jv.Bool(false))
      'n' -> literal("null", Jv.Null)
      else -> if (c == '-' || c in '0'..'9') Jv.Num(parseNumber()) else throw IllegalArgumentException("JSON 非法字符：$c")
    }
  }

  private fun literal(word: String, v: Jv): Jv {
    if (i + word.length > s.length || s.substring(i, i + word.length) != word) {
      throw IllegalArgumentException("JSON 非法字面量")
    }
    i += word.length
    return v
  }

  private fun parseObj(depth: Int): Jv {
    i++ // '{'
    val out = LinkedHashMap<String, Jv>()
    skipWs()
    if (!eof() && s[i] == '}') {
      i++
      return Jv.Obj(out)
    }
    while (true) {
      skipWs()
      val key = parseString()
      skipWs()
      if (eof() || s[i] != ':') throw IllegalArgumentException("JSON 缺少冒号")
      i++
      out[key] = parseValue(depth + 1)
      skipWs()
      if (eof()) throw IllegalArgumentException("JSON 对象未闭合")
      when (s[i]) {
        ',' -> i++
        '}' -> {
          i++
          return Jv.Obj(out)
        }
        else -> throw IllegalArgumentException("JSON 对象分隔符非法")
      }
    }
  }

  private fun parseArr(depth: Int): Jv {
    i++ // '['
    val out = ArrayList<Jv>()
    skipWs()
    if (!eof() && s[i] == ']') {
      i++
      return Jv.Arr(out)
    }
    while (true) {
      out.add(parseValue(depth + 1))
      skipWs()
      if (eof()) throw IllegalArgumentException("JSON 数组未闭合")
      when (s[i]) {
        ',' -> i++
        ']' -> {
          i++
          return Jv.Arr(out)
        }
        else -> throw IllegalArgumentException("JSON 数组分隔符非法")
      }
    }
  }

  private fun parseString(): String {
    if (eof() || s[i] != '"') throw IllegalArgumentException("JSON 期望字符串")
    i++
    val sb = StringBuilder()
    while (true) {
      if (eof()) throw IllegalArgumentException("JSON 字符串未闭合")
      val c = s[i++]
      when {
        c == '"' -> return sb.toString()
        c == '\\' -> {
          if (eof()) throw IllegalArgumentException("JSON 转义未闭合")
          when (val e = s[i++]) {
            '"' -> sb.append('"')
            '\\' -> sb.append('\\')
            '/' -> sb.append('/')
            'b' -> sb.append('\b')
            'f' -> sb.append('\u000C')
            'n' -> sb.append('\n')
            'r' -> sb.append('\r')
            't' -> sb.append('\t')
            'u' -> {
              if (i + 4 > s.length) throw IllegalArgumentException("JSON \\u 转义不完整")
              val hex = s.substring(i, i + 4)
              i += 4
              sb.append(hex.toInt(16).toChar())
            }
            else -> throw IllegalArgumentException("JSON 非法转义：$e")
          }
        }
        else -> sb.append(c)
      }
    }
  }

  private fun parseNumber(): Double {
    val start = i
    if (!eof() && s[i] == '-') i++
    while (!eof() && s[i] in '0'..'9') i++
    if (!eof() && s[i] == '.') {
      i++
      while (!eof() && s[i] in '0'..'9') i++
    }
    if (!eof() && (s[i] == 'e' || s[i] == 'E')) {
      i++
      if (!eof() && (s[i] == '+' || s[i] == '-')) i++
      while (!eof() && s[i] in '0'..'9') i++
    }
    val text = s.substring(start, i)
    return text.toDoubleOrNull() ?: throw IllegalArgumentException("JSON 非法数字：$text")
  }
}
