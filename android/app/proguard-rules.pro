# =============================================================================
# R8 / ProGuard 规则（release：isMinifyEnabled = true + isShrinkResources = true，
# 配置见 app/build.gradle.kts；only release 走这里，debug 不混淆）
#
# 原则：只保留「反射 / JNI 边界」的类；业务代码照常混淆，别为了省心整包 -keep，
#       否则 R8 白开（体积与启动都白涨）。
# 已知坑：JNI 边界一旦被 rename，只有 release 包才炸（debug 不复现），
#       典型报错是 UnsatisfiedLinkError / NoClassDefFoundError / NoSuchMethodError。
# =============================================================================

# --- libtorrent4j：JNI 边界（最要紧的一条）-----------------------------------
# swig 生成的类（org.libtorrent4j.swig.*）是 native 方法的宿主，C++ 侧按**字面
# 类名/方法名**回查（Java_org_libtorrent4j_swig_*）；类名或方法名被改就是崩。
# 另外 libtorrent4j 里有只被 native 代码 new 出来的类，R8 看不到引用会整类删掉。
-keep class org.libtorrent4j.** { *; }
-keep class org.libtorrent4j.swig.** { *; }
-dontwarn org.libtorrent4j.**

# --- AndroidX / Compose ------------------------------------------------------
# androidx 与 compose 的 AAR 都自带 consumer rules（AGP 自动并入），这里只兜底
# 「引用了可选/缺失类」的告警，避免 R8 直接因 missing class 失败。
-dontwarn androidx.**
-dontwarn com.google.android.material.**

# --- 播放器（Media3/ExoPlayer）-----------------------------------------------
# Media3 用 @UnstableApi + 反射装配 Renderer/Extractor；官方 rules 已随 AAR 下发，
# 这里只补不保留注解会丢的行为标记。
-keepattributes RuntimeVisibleAnnotations, RuntimeVisibleParameterAnnotations
-dontwarn androidx.media3.**

# --- Kotlin / kotlinx --------------------------------------------------------
# kotlinx-serialization 的生成器由编译插件注入（R8 规则随插件一起打包），
# 只需保证序列化器注解与伴生对象不被抹掉。
-keepclassmembers class **$Companion { *; }
-keepattributes *Annotation*, InnerClasses, Signature, EnclosingMethod
-dontwarn org.jetbrains.annotations.**

# --- 本项目 ------------------------------------------------------------------
# 枚举常量在 UI/持久化里按名字读写（TaskStates / CacheMode 等），保留枚举成员名，
# 否则 valueOf()/name() 的落盘值会随混淆漂移。
-keepclassmembers enum com.bitseed.magnetviewer.** {
    public static **[] values();
    public static ** valueOf(java.lang.String);
    public java.lang.String name();
}
