package frameport.civ;

import android.app.NativeActivity;
import android.content.Context;
import android.os.storage.StorageManager;
import android.util.Log;

public final class FrameNativeActivity extends NativeActivity {
    private FrameStorageManager frameStorage;
    private ClassLoader legacyParent;
    @Override public synchronized ClassLoader getClassLoader() {
        // NativeActivity must load libmain with its normal loader first. The game's
        // explicit DexClassLoader is constructed only after requesting storage.
        if (frameStorage == null) return super.getClassLoader();
        if (legacyParent == null) legacyParent = new FrameClassLoaderParent(super.getClassLoader());
        return legacyParent;
    }
    @Override public synchronized Object getSystemService(String name) {
        if (!Context.STORAGE_SERVICE.equals(name)) return super.getSystemService(name);
        if (frameStorage == null) {
            StorageManager original = (StorageManager) super.getSystemService(name);
            frameStorage = new FrameStorageManager(this, original, getObbDir());
            Log.i("FramePortObb", "directory-backed expansion loader installed");
        }
        return frameStorage;
    }
}
