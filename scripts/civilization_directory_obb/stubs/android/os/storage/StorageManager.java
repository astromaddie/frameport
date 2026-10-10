package android.os.storage;
import android.content.Context;
import android.os.Looper;
/** Compile-only signature for Android's public hidden constructor; never dexed. */
public class StorageManager {
    public StorageManager(Context context, Looper looper) { }
    public boolean mountObb(String path, String key, OnObbStateChangeListener listener) { return false; }
    public boolean unmountObb(String path, boolean force, OnObbStateChangeListener listener) { return false; }
    public boolean isObbMounted(String path) { return false; }
    public String getMountedObbPath(String path) { return null; }
}
