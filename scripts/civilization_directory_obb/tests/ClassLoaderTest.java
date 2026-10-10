import frameport.civ.FrameClassLoaderParent;
import java.net.URL;
import java.net.URLClassLoader;

public final class ClassLoaderTest {
    private static final class ExplicitLoader extends URLClassLoader {
        ExplicitLoader(URL url, ClassLoader parent) { super(new URL[]{url}, parent); }
        Class<?> explicitFind(String name) throws ClassNotFoundException { return findClass(name); }
    }
    public static void main(String[] args) throws Exception {
        URL url = new java.io.File(args[0]).toURI().toURL();
        try (URLClassLoader original = new URLClassLoader(new URL[]{url}, ClassLoader.getSystemClassLoader());
             ExplicitLoader unfiltered = new ExplicitLoader(url, original);
             ExplicitLoader filtered = new ExplicitLoader(url, new FrameClassLoaderParent(original))) {
            boolean reproduced = false;
            try { unfiltered.explicitFind("com.firaxis.obb.LoaderFixture").getConstructor().newInstance(); }
            catch (java.lang.reflect.InvocationTargetException error) {
                reproduced = error.getCause() instanceof IllegalAccessError;
            }
            if (!reproduced) throw new AssertionError("original mixed-loader failure not reproduced");
            Object result = filtered.explicitFind("com.firaxis.obb.LoaderFixture").getConstructor().newInstance();
            if (result == null || result.getClass().getClassLoader() != filtered)
                throw new AssertionError("helper did not remain in native child loader");
            if (filtered.loadClass("java.lang.String") != String.class)
                throw new AssertionError("framework delegation changed");
        }
        System.out.println("Class-loader regression reproduced; filtered parent passed");
    }
}
