package app.vaani

import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Shader
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import kotlin.math.hypot

/** Lets a view be dragged around the screen; the service moves the window and remembers where it was left. */
interface Drag { fun move(v: View, dx: Int, dy: Int); fun end(v: View) }

/** The round button shown beside a focused text field: the Vaani mark (wave and sound bars) on the app icon's purple. */
class BubbleView(context: Context, private val drag: Drag, private val onTap: () -> Unit) : View(context) {
    private val slop = ViewConfiguration.get(context).scaledTouchSlop
    private var downX = 0f; private var downY = 0f; private var lastX = 0f; private var lastY = 0f; private var dragging = false
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private var pressed = false
    private val mark = BitmapFactory.decodeResource(resources, R.drawable.vaani_mark)

    init {
        contentDescription = "Dictate with Vaani"
        isClickable = true
        isHapticFeedbackEnabled = true
    }

    override fun onDraw(c: Canvas) {
        val w = width.toFloat()
        val d = resources.displayMetrics.density
        val r = 18 * d                      // 36 dp circle inside the 48 dp touch area
        paint.reset(); paint.isAntiAlias = true
        paint.shader = LinearGradient(w / 2 - r, w / 2 - r, w / 2 + r, w / 2 + r, 0xFF792AB5.toInt(), 0xFF1D0D36.toInt(), Shader.TileMode.CLAMP)
        c.drawCircle(w / 2, w / 2, r, paint)
        paint.shader = null
        paint.style = Paint.Style.STROKE; paint.strokeWidth = d
        paint.color = 0x33FFFFFF
        c.drawCircle(w / 2, w / 2, r - d / 2, paint)
        // the mark, about two thirds of the circle wide
        val mw = r * 1.3f
        val mh = mw * mark.height / mark.width
        paint.reset(); paint.isFilterBitmap = true; paint.alpha = if (pressed) 200 else 255
        c.drawBitmap(mark, null, RectF(w / 2 - mw / 2, w / 2 - mh / 2, w / 2 + mw / 2, w / 2 + mh / 2), paint)
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        when (e.action) {
            MotionEvent.ACTION_DOWN -> { pressed = true; dragging = false; downX = e.rawX; downY = e.rawY; lastX = e.rawX; lastY = e.rawY; invalidate() }
            MotionEvent.ACTION_MOVE -> {
                if (!dragging && hypot(e.rawX - downX, e.rawY - downY) > slop) dragging = true
                if (dragging) {
                    val dx = (e.rawX - lastX).toInt(); val dy = (e.rawY - lastY).toInt()
                    lastX += dx; lastY += dy
                    drag.move(this, dx, dy)
                }
            }
            MotionEvent.ACTION_UP -> {
                pressed = false
                invalidate()
                if (dragging) drag.end(this) else {
                    performHapticFeedback(android.view.HapticFeedbackConstants.KEYBOARD_TAP)
                    onTap()
                }
            }
            MotionEvent.ACTION_CANCEL -> { pressed = false; invalidate(); if (dragging) drag.end(this) }
        }
        return true
    }
}
