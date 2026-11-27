package app.vaani

import android.Manifest
import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.Crossfade
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.io.File
import java.text.DateFormat
import java.util.Date

/** Everything the screens show. Compose reads these values and redraws when they change. */
class AppState(val ctx: Context) {
    private val ui = Handler(Looper.getMainLooper())
    var tab by mutableStateOf(0)
    var toast by mutableStateOf<String?>(null)
    var query by mutableStateOf("")
    val history = mutableStateListOf<Entry>()
    val notes = mutableStateListOf<Note>()
    var serviceOn by mutableStateOf(false)
    var micOk by mutableStateOf(false)
    var batteryOk by mutableStateOf(true)
    var lang by mutableStateOf("or")
    var rev by mutableStateOf(0)
    val progress = mutableStateMapOf<String, Float>()
    private val prefs get() = ctx.getSharedPreferences("flow", Context.MODE_PRIVATE)

    fun refresh() {
        serviceOn = (Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: "")
            .contains(FlowService::class.java.name)
        micOk = ctx.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as? PowerManager
        batteryOk = pm?.isIgnoringBatteryOptimizations(ctx.packageName) ?: true
        lang = prefs.getString("lang", "or") ?: "or"
        history.clear(); history.addAll(Db.get(ctx).all())
        notes.clear(); notes.addAll(Db.get(ctx).notes())
        rev++
    }

    fun chooseLang(l: String) { prefs.edit().putString("lang", l).apply(); lang = l }
    fun resetPosition() { prefs.edit().remove("offRight").remove("offUp").apply(); say("Button is back at the right edge") }
    fun say(msg: String) { toast = msg; ui.postDelayed({ if (toast == msg) toast = null }, 1800) }
    fun copy(text: String) {
        (ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("Vaani", text))
        say("Copied")
    }

    fun share(text: String) {
        val send = Intent(Intent.ACTION_SEND).apply { type = "text/plain"; putExtra(Intent.EXTRA_TEXT, text) }
        ctx.startActivity(Intent.createChooser(send, "Share note").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }

    /** The chosen language's model folder, or null after sending the user to the Models tab to download it. */
    private fun modelDir(): File? {
        val m = Models.all.firstOrNull { it.id == lang }
        if (m == null || !Models.installed(ctx, m)) {
            say("Download the ${langName(lang)} model first")
            tab = 2
            return null
        }
        return Models.dir(ctx, lang)
    }

    /** One loaded speech model, shared by the playground and notes (loading one takes seconds and ~200 MB). */
    @Synchronized private fun engine(l: String, dir: File): SpeechEngine {
        sandboxEngine?.let { if (it.lang == l) return it }
        sandboxEngine?.close()
        return SpeechEngine(l, dir).also { sandboxEngine = it }
    }

    var sandboxText by mutableStateOf("")
    var isRecordingSandbox by mutableStateOf(false)
    var sandboxLevel by mutableStateOf(0f)
    var sandboxStatus by mutableStateOf<String?>(null)

    private var sandboxRecorder: LevelRecorder? = null
    private var sandboxEngine: SpeechEngine? = null
    @Volatile private var sandboxStreaming = false

    fun toggleSandbox(activity: Activity) {
        if (isRecordingSandbox) {
            stopSandbox()
        } else {
            startSandbox(activity)
        }
    }

    fun clearSandbox() {
        sandboxText = ""
        sandboxStatus = null
    }

    private fun startSandbox(activity: Activity) {
        if (!micOk) {
            activity.requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
            say("Microphone permission needed")
            return
        }
        if (noteRecording) { say("Stop the note first"); return }
        val dir = modelDir() ?: return
        isRecordingSandbox = true
        sandboxStatus = "Listening..."
        sandboxText = ""

        val r = LevelRecorder { lvl ->
            ui.post { sandboxLevel = lvl }
        }
        if (!r.start()) {
            isRecordingSandbox = false
            sandboxStatus = null
            say("Microphone is busy")
            return
        }
        sandboxRecorder = r
        sandboxStreaming = true

        Thread {
            runCatching { engine(lang, dir) }.onFailure {
                ui.post {
                    isRecordingSandbox = false
                    sandboxStatus = null
                    say("Failed to load speech engine")
                }
                return@Thread
            }

            var committed = ""
            var tailStart = 0
            while (sandboxStreaming) {
                Thread.sleep(500)
                val e = sandboxEngine ?: continue
                val cur = r.length
                if (cur - tailStart < 4800) continue
                val t = if (r.peak(tailStart, cur) < 0.006f) "" else runCatching { e.transcribe(r.samples(tailStart, cur)) }.getOrDefault("")
                if ((r.quietFor(700) && cur - tailStart > 32000) || cur - tailStart > 16000 * 12) {
                    if (t.isNotEmpty()) committed += "$t "
                    tailStart = cur
                    val formatted = TextFormatter.format(committed, lang, isFinal = false)
                    ui.post { sandboxText = formatted }
                } else {
                    val formatted = TextFormatter.format(committed + t, lang, isFinal = false)
                    ui.post { sandboxText = formatted }
                }
            }

            // Final pass
            Thread.sleep(150)
            val cur = r.length
            val e = sandboxEngine
            val last = if (e != null && cur - tailStart >= 1600 && r.peak(tailStart, cur) >= 0.006f) {
                runCatching { e.transcribe(r.samples(tailStart, cur), isFinal = true) }.getOrDefault("")
            } else ""
            val rawFinal = (committed + last).trim()
            val formattedFinal = TextFormatter.format(rawFinal, lang, isFinal = true)
            ui.post {
                isRecordingSandbox = false
                sandboxStatus = null
                sandboxLevel = 0f
                if (formattedFinal.isNotEmpty()) {
                    sandboxText = formattedFinal
                    Db.get(ctx).add(lang, formattedFinal)
                    refresh()
                    say("Dictation saved to History")
                }
            }
        }.start()
    }

    private fun stopSandbox() {
        sandboxStreaming = false
        sandboxRecorder?.stop()
        sandboxRecorder = null
        sandboxStatus = "Processing..."
    }

    // -- notes: a call or lecture, one line per sentence -------------------------------------------

    val noteLines = mutableStateListOf<String>()
    var noteLive by mutableStateOf("")       // the sentence being heard right now
    var noteRecording by mutableStateOf(false)
    var noteSeconds by mutableStateOf(0)
    var noteLevel by mutableStateOf(0f)
    private var noteRec: LevelRecorder? = null
    @Volatile private var noteRunning = false

    fun toggleNotes(activity: Activity) = if (noteRecording) stopNotes() else startNotes(activity)

    private fun startNotes(activity: Activity) {
        if (isRecordingSandbox) { say("Stop the dictation first"); return }
        if (!micOk) {
            activity.requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
            say("Microphone permission needed")
            return
        }
        val dir = modelDir() ?: return
        val noteLang = lang
        var r = LevelRecorder { l -> ui.post { noteLevel = l } }
        if (!r.start()) { say("Microphone is busy"); return }
        noteLines.clear(); noteLive = ""; noteSeconds = 0
        noteRec = r; noteRunning = true; noteRecording = true
        val started = System.currentTimeMillis()
        ui.post(object : Runnable {  // the clock runs from the tap, not from when the model finishes loading
            override fun run() {
                if (!noteRecording) return
                noteSeconds = ((System.currentTimeMillis() - started) / 1000).toInt()
                ui.postDelayed(this, 500)
            }
        })

        Thread {
            val e = runCatching { engine(noteLang, dir) }.getOrElse {
                noteRunning = false; r.stop()
                ui.post { noteRecording = false; say("Failed to load speech engine") }
                return@Thread
            }
            fun line(text: String) {
                if (text.isNotBlank()) {
                    val formatted = TextFormatter.format(text, noteLang, isFinal = true)
                    ui.post { noteLines.add(formatted); noteLive = "" }
                }
            }
            var tail = 0
            while (noteRunning) {
                Thread.sleep(500)
                val cur = r.length
                if (cur - tail < 8000) continue
                val heard = r.peak(tail, cur) >= 0.006f
                if ((r.quietFor(700) && cur - tail > 32000) || cur - tail > 16000 * 12) {  // a sentence ended, or 12 s passed
                    line(if (heard) runCatching { e.transcribe(r.samples(tail, cur), isFinal = true) }.getOrDefault("") else "")
                    tail = cur
                    if (cur > 16000 * 75) {
                        // ponytail: the recorder holds 90 s, so a fresh one starts at a sentence break (~50 ms unheard)
                        r.stop()
                        r = LevelRecorder { l -> ui.post { noteLevel = l } }
                        if (!r.start()) { noteRunning = false; ui.post { say("Microphone is busy") } }
                        noteRec = r
                        tail = 0
                    }
                } else if (heard) {
                    val t = runCatching { e.transcribe(r.samples(tail, cur)) }.getOrDefault("")
                    ui.post { noteLive = t }
                }
            }
            Thread.sleep(150)
            val cur = r.length
            if (cur - tail >= 1600 && r.peak(tail, cur) >= 0.006f)
                line(runCatching { e.transcribe(r.samples(tail, cur), isFinal = true) }.getOrDefault(""))
            ui.post {
                noteRecording = false; noteLevel = 0f; noteLive = ""
                val text = noteLines.joinToString("\n")
                if (text.isNotBlank()) {
                    Db.get(ctx).addNote(noteLang, noteSeconds, text)
                    refresh()
                    say("Note saved")
                } else say("Nothing was heard")
            }
        }.start()
    }

    private fun stopNotes() {
        noteRunning = false
        noteRec?.stop()
        noteRec = null
    }
}

@Composable
fun App(s: AppState) {
    Box(Modifier.fillMaxSize().background(C.bg).windowInsetsPadding(WindowInsets.systemBars)) {
        Column(Modifier.fillMaxSize()) {
            Box(Modifier.weight(1f)) {
                Crossfade(s.tab, label = "tab") { t ->
                    when (t) { 0 -> HistoryScreen(s); 1 -> NotesScreen(s); 2 -> ModelsScreen(s); else -> SettingsScreen(s) }
                }
            }
            TabBar(s.tab) { s.tab = it }
        }
        AnimatedVisibility(s.toast != null, Modifier.align(Alignment.BottomCenter).padding(bottom = 84.dp),
            enter = fadeIn() + slideInVertically { it / 2 }, exit = fadeOut() + slideOutVertically { it / 2 }) {
            Box(Modifier.clip(RoundedCornerShape(50)).background(C.surface2).padding(horizontal = 20.dp, vertical = 12.dp)) { T(s.toast ?: "", 15, FontWeight.Medium) }
        }
    }
}

private fun ago(ts: Long): String {
    val m = (System.currentTimeMillis() - ts) / 60000
    return when { m < 1 -> "just now"; m < 60 -> "$m min ago"; m < 1440 -> "${m / 60} h ago"
        else -> DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date(ts)) }
}

private fun langName(l: String) = when (l) { "or" -> "Odia"; "en" -> "English"; "hi" -> "Hindi"; else -> l }
private fun mmss(sec: Int) = "%d:%02d".format(sec / 60, sec % 60)

@Composable
private fun LevelBars(level: Float) =
    Row(Modifier.fillMaxWidth().padding(bottom = 12.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        for (i in 0 until 14) {
            val barHeight = (4.dp + (level * 22f * ((i % 4 + 1) / 3f)).dp).coerceIn(4.dp, 26.dp)
            Box(Modifier.weight(1f).height(barHeight).clip(RoundedCornerShape(2.dp)).background(if (i < 10) C.accent else C.red))
        }
    }

// ------------------------------------------------------------------ History

@Composable
fun DictationPlayground(s: AppState) {
    val ctx = LocalContext.current
    val activity = ctx as? Activity
    Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 14.dp)) {
        Column(Modifier.padding(18.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                T("DICTATION PLAYGROUND", 12, FontWeight.Bold, c = C.accent, font = AkiraExpanded)
                Box(Modifier.clip(RoundedCornerShape(6.dp)).background(C.surface2).padding(horizontal = 8.dp, vertical = 4.dp)) {
                    T(langName(s.lang).uppercase(), 11, FontWeight.Bold, c = C.text2)
                }
            }

            Box(Modifier.fillMaxWidth().padding(vertical = 12.dp).clip(RoundedCornerShape(12.dp)).background(C.bg).padding(14.dp)) {
                if (s.sandboxText.isEmpty() && !s.isRecordingSandbox) {
                    T("Tap the microphone and speak to test offline dictation in real time.", 15, c = C.text3, line = 22)
                } else {
                    T(if (s.sandboxText.isEmpty()) "Listening..." else s.sandboxText, 16, c = if (s.sandboxText.isEmpty()) C.text2 else C.text, line = 24)
                }
            }

            if (s.isRecordingSandbox) LevelBars(s.sandboxLevel)

            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (s.sandboxText.isNotEmpty() && !s.isRecordingSandbox) {
                        PillButton("Copy", primary = false, small = true) { s.copy(s.sandboxText) }
                        PillButton("Clear", primary = false, small = true) { s.clearSandbox() }
                    }
                }
                PillButton(
                    if (s.isRecordingSandbox) "Stop" else "Start Dictation",
                    primary = !s.isRecordingSandbox,
                    small = true
                ) {
                    activity?.let { s.toggleSandbox(it) }
                }
            }
        }
    }
}

@Composable
fun HistoryScreen(s: AppState) {
    val ctx = LocalContext.current
    val shown = s.history.filter { s.query.isBlank() || it.text.contains(s.query, ignoreCase = true) }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 24.dp)) {
        item { Title("History", "Everything you dictated. Tap one to copy it.") }
        item { DictationPlayground(s) }
        if (!s.serviceOn) item {
            Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 12.dp)) {
                Column(Modifier.padding(16.dp)) {
                    T("Turn on the Vaani button", 17, FontWeight.SemiBold)
                    T("It shows a small button next to any text box. Android asks you to allow it once.", 14, c = C.text2, modifier = Modifier.padding(top = 4.dp, bottom = 12.dp))
                    PillButton("Open accessibility settings", small = true) { ctx.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
                }
            }
        }
        item {
            Box(Modifier.padding(horizontal = 16.dp).padding(bottom = 12.dp).fillMaxWidth().height(44.dp).clip(RoundedCornerShape(50)).background(C.surface)
                .padding(horizontal = 18.dp), contentAlignment = Alignment.CenterStart) {
                BasicTextField(s.query, { s.query = it }, singleLine = true, cursorBrush = SolidColor(C.accent),
                    textStyle = TextStyle(color = C.text, fontSize = 16.sp, fontFamily = Inter), modifier = Modifier.fillMaxWidth(),
                    decorationBox = { inner -> if (s.query.isEmpty()) T("Search", 16, c = C.text3); inner() })
            }
        }
        if (shown.isEmpty()) item {
            T(if (s.history.isEmpty()) "Nothing yet. Tap into a text box in any app, tap the Vaani button and speak."
              else "No match.", 15, c = C.text2, modifier = Modifier.padding(horizontal = 24.dp, vertical = 32.dp), line = 22)
        }
        items(shown, key = { it.id }) { e ->
            Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 10.dp).press { s.copy(e.text) }) {
                Column(Modifier.padding(16.dp)) {
                    T(e.text, 16, max = 4, line = 23)
                    Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        T("${langName(e.lang)}  ·  ${ago(e.ts)}", 13, c = C.text3)
                        Box(Modifier.heightIn48().press { Db.get(ctx).delete(e.id); s.refresh() }, contentAlignment = Alignment.CenterEnd) { T("Delete", 14, FontWeight.Medium, C.red) }
                    }
                }
            }
        }
    }
}

private fun Modifier.heightIn48() = this.then(Modifier.height(36.dp))

// ------------------------------------------------------------------ Notes (meetings, lectures)

@Composable
fun NotesScreen(s: AppState) {
    val ctx = LocalContext.current
    val activity = ctx as? Activity
    val view = LocalView.current
    DisposableEffect(s.noteRecording) {  // a long lecture must not end because the screen went to sleep
        view.keepScreenOn = s.noteRecording
        onDispose { view.keepScreenOn = false }
    }
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 24.dp)) {
        item { Title("Notes", "Record a call or a lecture. Each sentence lands on its own line.") }
        item {
            Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 6.dp)) {
                Column(Modifier.padding(18.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        T(if (s.noteRecording) "RECORDING" else "NEW NOTE", 12, FontWeight.Bold, c = if (s.noteRecording) C.red else C.accent, font = AkiraExpanded)
                        Box(Modifier.clip(RoundedCornerShape(6.dp)).background(C.surface2).padding(horizontal = 8.dp, vertical = 4.dp)) {
                            T(if (s.noteRecording) mmss(s.noteSeconds) else langName(s.lang).uppercase(), 11, FontWeight.Bold, c = C.text2)
                        }
                    }
                    Column(Modifier.fillMaxWidth().padding(vertical = 12.dp).heightIn(min = 96.dp).clip(RoundedCornerShape(12.dp)).background(C.bg).padding(14.dp)) {
                        if (s.noteLines.isEmpty() && s.noteLive.isEmpty()) {
                            T(if (s.noteRecording) "Listening..." else "Tap Record and keep the phone near whoever is speaking. Keep Vaani open while it records; speech stays on this phone.",
                                15, c = if (s.noteRecording) C.text2 else C.text3, line = 22)
                        } else {
                            s.noteLines.forEach { T(it, 16, line = 23, modifier = Modifier.padding(bottom = 6.dp)) }
                            if (s.noteLive.isNotEmpty()) T(s.noteLive, 16, c = C.text2, line = 23)
                        }
                    }
                    if (s.noteRecording) LevelBars(s.noteLevel)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                        PillButton(if (s.noteRecording) "Stop and Save" else "Record", primary = !s.noteRecording, small = true) {
                            activity?.let { s.toggleNotes(it) }
                        }
                    }
                }
            }
        }
        if (s.notes.isNotEmpty()) item { SectionLabel("Saved notes") }
        items(s.notes, key = { it.id }) { n ->
            Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 10.dp).press { s.copy(n.text) }) {
                Column(Modifier.padding(16.dp)) {
                    T(n.text, 16, max = 6, line = 23)
                    Row(Modifier.fillMaxWidth().padding(top = 10.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        T("${langName(n.lang)}  ·  ${mmss(n.seconds)}  ·  ${ago(n.ts)}", 13, c = C.text3)
                        Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
                            Box(Modifier.heightIn48().press { s.share(n.text) }, contentAlignment = Alignment.CenterEnd) { T("Share", 14, FontWeight.Medium, C.accent) }
                            Box(Modifier.heightIn48().press { Db.get(ctx).deleteNote(n.id); s.refresh() }, contentAlignment = Alignment.CenterEnd) { T("Delete", 14, FontWeight.Medium, C.red) }
                        }
                    }
                }
            }
        }
    }
}

// ------------------------------------------------------------------ Models

@Composable
fun ModelsScreen(s: AppState) {
    val ctx = LocalContext.current
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 24.dp)) {
        item { Title("Models", "Speech models live on this phone. Download once, then it works offline.") }
        items(Models.all, key = { it.id }) { m ->
            val installed = remember(s.rev) { Models.installed(ctx, m) }
            val p = s.progress[m.id]
            Group(Modifier.padding(horizontal = 16.dp).padding(bottom = 10.dp)) {
                Column(Modifier.padding(16.dp)) {
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f).padding(end = 12.dp)) {
                            T(m.title, 17, FontWeight.SemiBold)
                            T(m.note, 14, c = C.text2, modifier = Modifier.padding(top = 3.dp), line = 20)
                            T("${m.sizeMb} MB", 13, c = C.text3, modifier = Modifier.padding(top = 6.dp))
                        }
                        when {
                            p != null -> T("${(p * 100).toInt()}%", 15, FontWeight.Medium, C.accent)
                            installed -> PillButton("Delete", primary = false, small = true) { Models.delete(ctx, m); s.refresh(); s.say("${m.title} removed") }
                            else -> PillButton("Download", small = true) {
                                s.progress[m.id] = 0f
                                Thread {
                                    Models.download(ctx, m) { s.progress[m.id] = it }
                                        .onSuccess { Handler(Looper.getMainLooper()).post { s.progress.remove(m.id); s.refresh(); s.say("${m.title} is ready") } }
                                        .onFailure { Handler(Looper.getMainLooper()).post { s.progress.remove(m.id); s.say("Download failed. Check your connection.") } }
                                }.start()
                            }
                        }
                    }
                    if (p != null) Box(Modifier.padding(top = 12.dp).fillMaxWidth().height(4.dp).clip(RoundedCornerShape(50)).background(C.surface2)) {
                        Box(Modifier.fillMaxWidth(p.coerceIn(0.02f, 1f)).height(4.dp).clip(RoundedCornerShape(50)).background(C.accent))
                    }
                }
            }
        }

    }
}

// ------------------------------------------------------------------ Settings

@Composable
private fun SettingRow(title: String, value: String, ok: Boolean? = null, action: (@Composable () -> Unit)? = null) =
    Row(Modifier.fillMaxWidth().heightIn48x().padding(horizontal = 16.dp, vertical = 10.dp), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            T(title, 16)
            T(value, 13, c = if (ok == true) C.green else C.text3)
        }
        action?.invoke()
    }

private fun Modifier.heightIn48x() = this.then(Modifier.height(64.dp))

@Composable
fun SettingsScreen(s: AppState) {
    val ctx = LocalContext.current
    LazyColumn(Modifier.fillMaxSize(), contentPadding = androidx.compose.foundation.layout.PaddingValues(bottom = 24.dp)) {
        item { Title("Settings", "") }
        item { SectionLabel("Set up") }
        item {
            Group(Modifier.padding(horizontal = 16.dp)) {
                SettingRow("Vaani button", if (s.serviceOn) "On" else "Off", s.serviceOn) {
                    if (!s.serviceOn) PillButton("Turn on", small = true) { ctx.startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
                }
                Sep()
                SettingRow("Microphone", if (s.micOk) "Allowed" else "Not allowed yet", s.micOk) {
                    if (!s.micOk) PillButton("Allow", small = true) { (ctx as Activity).requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1) }
                }
                Sep()
                SettingRow("Background battery", if (s.batteryOk) "Unrestricted" else "Optimized (may sleep)", s.batteryOk) {
                    if (!s.batteryOk) PillButton("Keep alive", small = true) {
                        runCatching {
                            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                                data = Uri.parse("package:${ctx.packageName}")
                            }
                            ctx.startActivity(intent)
                        }.onFailure {
                            ctx.startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
                        }
                    }
                }
            }
        }
        item { SectionLabel("Speak in") }
        item {
            Group(Modifier.padding(horizontal = 16.dp)) {
                listOf("or" to "Odia", "en" to "English", "hi" to "Hindi").forEachIndexed { i, (code, name) ->
                    if (i > 0) Sep()
                    val on = s.lang == code
                    Row(Modifier.fillMaxWidth().height(56.dp).press { s.chooseLang(code) }.padding(horizontal = 16.dp),
                        horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                        T(name, 16)
                        if (on) T("Selected", 14, FontWeight.Medium, C.accent)
                    }
                }
            }
        }
        item { SectionLabel("Button") }
        item {
            Group(Modifier.padding(horizontal = 16.dp)) {
                SettingRow("Position", "Drag the button anywhere; it remembers.") { PillButton("Reset", primary = false, small = true) { s.resetPosition() } }
            }
        }
        item { SectionLabel("History") }
        item {
            Group(Modifier.padding(horizontal = 16.dp)) {
                SettingRow("Clear history", "${s.history.size} saved dictations") {
                    PillButton("Clear", primary = false, small = true) { Db.get(ctx).clear(); s.refresh(); s.say("History cleared") }
                }
            }
        }
        item { SectionLabel("Privacy") }
        item {
            Group(Modifier.padding(horizontal = 16.dp)) {
                T("Speech is recognised on this phone. The only time Vaani uses the internet is when you tap Download on a model. " +
                    "The accessibility permission is used to notice a focused text box and to type your words; nothing on your screen is stored or sent.",
                    14, c = C.text2, modifier = Modifier.padding(16.dp), line = 21)
            }
        }
    }
}
