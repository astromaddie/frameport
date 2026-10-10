package frameport.civ;

import android.content.Context;
import android.os.Looper;
import android.os.storage.OnObbStateChangeListener;
import android.os.storage.StorageManager;
import android.util.Log;
import java.io.File;
import java.util.HashSet;
import java.util.Set;

/** Preserve Android's OBB API while serving verified directory copies on Lepton. */
public final class FrameStorageManager extends StorageManager {
    private final StorageManager original;
    private final DirectoryMount directoryMount;
    private final Set<String> mounted = new HashSet<String>();
    public FrameStorageManager(Context context, StorageManager original, File root) {
        super(context, Looper.getMainLooper());
        this.original = original;
        this.directoryMount = new DirectoryMount(root);
    }
    @Override public synchronized boolean mountObb(String path, String key, OnObbStateChangeListener listener) {
        String directory = directoryMount.resolve(path, key);
        if (directory == null) {
            Log.w("FramePortObb", "directory mount unavailable: " + directoryMount.describe(path, key));
            return original.mountObb(path, key, listener);
        }
        if (listener == null) throw new NullPointerException("listener");
        mounted.add(path);
        Log.i("FramePortObb", "directory mount ready: " + path);
        listener.onObbStateChange(path, OnObbStateChangeListener.MOUNTED);
        return true;
    }
    @Override public synchronized String getMountedObbPath(String path) {
        String directory = mounted.contains(path) ? directoryMount.resolve(path, null) : null;
        return directory != null ? directory : original.getMountedObbPath(path);
    }
    @Override public synchronized boolean isObbMounted(String path) {
        return (mounted.contains(path) && directoryMount.resolve(path, null) != null) || original.isObbMounted(path);
    }
    @Override public synchronized boolean unmountObb(String path, boolean force, OnObbStateChangeListener listener) {
        if (!mounted.remove(path)) return original.unmountObb(path, force, listener);
        if (listener != null) listener.onObbStateChange(path, OnObbStateChangeListener.UNMOUNTED);
        return true;
    }
}
