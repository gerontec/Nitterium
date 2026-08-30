package com.kaleedtc.nitterium

import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.lifecycleScope
import com.kaleedtc.nitterium.data.repository.UserPreferencesRepository
import com.kaleedtc.nitterium.ui.NitteriumApp
import com.kaleedtc.nitterium.ui.common.LocalFullScreenMode
import com.kaleedtc.nitterium.ui.common.viewModelFactory
import com.kaleedtc.nitterium.ui.theme.NitteriumTheme
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private val intentUrl = mutableStateOf<String?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        intentUrl.value = if (nimmSchluessel(intent)) null else intent?.dataString

        val app = application as NitteriumApplication
        val viewModel: MainViewModel by viewModels {
            viewModelFactory {
                MainViewModel(app.userPreferencesRepository)
            }
        }

        setContent {
            val uiState by viewModel.uiState.collectAsStateWithLifecycle()
            
            if (uiState.isLoading) {
                return@setContent
            }

            val fullScreenMode = remember { mutableStateOf(false) }

            // Handle System Bars visibility
            LaunchedEffect(fullScreenMode.value) {
                val window = this@MainActivity.window
                val insetsController = WindowCompat.getInsetsController(window, window.decorView)
                if (fullScreenMode.value) {
                    insetsController.hide(WindowInsetsCompat.Type.systemBars())
                    insetsController.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                } else {
                    insetsController.show(WindowInsetsCompat.Type.systemBars())
                }
            }

            val darkTheme = when (uiState.isDarkTheme) {
                true -> true
                false -> false
                null -> isSystemInDarkTheme()
            }

            CompositionLocalProvider(LocalFullScreenMode provides fullScreenMode) {
                NitteriumTheme(
                    darkTheme = darkTheme,
                    dynamicColor = uiState.isDynamicColor,
                    trueBlack = uiState.isTrueBlack
                ) {
                    NitteriumApp(
                        app = app,
                        isDarkTheme = darkTheme,
                        initialIntentUrl = intentUrl.value,
                        onIntentHandled = { intentUrl.value = null },
                        showNavLabels = uiState.showNavLabels,
                        defaultTab = uiState.defaultTab
                    )
                }
            }
        }
    }

    override fun onNewIntent(intent: android.content.Intent) {
        super.onNewIntent(intent)
        intentUrl.value = if (nimmSchluessel(intent)) null else intent.dataString
    }

    /**
     * Nimmt einen Zugangsschluessel aus einem Verweis entgegen:
     * `nitterium://instance?url=https://nitter.example&key=...`
     *
     * Gedacht fuer QR-Codes und Einrichtungsverweise - 32 Zeichen abzutippen
     * ist auf dem Telefon fehleranfaellig. Gibt true zurueck, wenn der Verweis
     * ein Schluessel war und nicht als Seite geoeffnet werden soll.
     */
    private fun nimmSchluessel(intent: android.content.Intent?): Boolean {
        val data = intent?.data ?: return false
        if (data.scheme != "nitterium" || data.host != "instance") return false
        val url = data.getQueryParameter("url") ?: data.getQueryParameter("host") ?: return true
        val key = data.getQueryParameter("key").orEmpty()
        val app = application as NitteriumApplication
        lifecycleScope.launch {
            app.userPreferencesRepository.setInstanceKey(url, key)
        }
        Toast.makeText(
            this,
            getString(R.string.instance_key_imported, UserPreferencesRepository.hostOf(url)),
            Toast.LENGTH_LONG
        ).show()
        return true
    }
}