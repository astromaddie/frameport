package android.app;

import android.content.Context;
import java.io.File;

/** Compile-only Android NativeActivity surface used by the directory OBB helper. */
public class NativeActivity extends Context {
    @Override public ClassLoader getClassLoader() { return super.getClassLoader(); }
    @Override public Object getSystemService(String name) { return super.getSystemService(name); }
    public File getObbDir() { return null; }
}
