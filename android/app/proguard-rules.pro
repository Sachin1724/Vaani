# ── Vaani Android ProGuard/R8 rules ────────────────────────────────────────

# ONNX Runtime — JNI bridge classes must not be stripped or renamed
-keep class ai.onnxruntime.** { *; }
-keepclassmembers class ai.onnxruntime.** { *; }

# Kotlin coroutines (used indirectly by Compose internals)
-keepnames class kotlinx.coroutines.** { *; }

# Compose compiler artefacts — keep lambda metadata for recomposition
-keep class androidx.compose.** { *; }
-keepclassmembers class androidx.compose.** { *; }

# Suppress noisy R8 notes about missing Kotlin metadata
-dontnote kotlin.**
-dontnote kotlinx.**

# Keep our own public API surface (accessibility service entry point)
-keep class app.vaani.FlowService { *; }
-keep class app.vaani.TextFormatter { *; }
