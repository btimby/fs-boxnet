"""
fs.contrib.boxdotnetfs
========

A FS object that integrates with box.net.

"""

# This might be a better API client, or something to use as a starting point.
# The official API client is horrible.
#
# https://bitbucket.org/paulc/boxcli/src/0480e1ae87c7/boxcli.py

# API (v1) Reference:
# http://developers.box.net/w/page/12923925/ApiFunction_create_folder

import time
import stat
import base64
import urllib
import httplib
import zipfile
import urlparse
import optparse
import datetime
import mimetools
import mimetypes
import os.path

from fs.base import *
from fs.path import *
from fs.errors import *
from fs.filelike import StringIO

from fs.contrib.dropboxfs import SpooledWriter as BaseSpooledWriter

import boxdotnet

AUTHURL = 'https://www.box.net/api/1.0/auth/%s'
DLURL = 'https://www.box.net/api/1.0/download'
ULURL = 'http://upload.box.net/api/1.0/upload'
XMLNODE_IGNORE = (
    'attrib', 'parseXML', 'xml',
    'elementName', 'elementText',
)
MULTIPART_HEAD = """--%(boundary)s
Content-Disposition: form-data; name="file"; filename="%(name)s"
Content-Type: %(mime)s

"""
MULTIPART_TAIL = """--%(boundary)s--
"""
def xml_to_tree(path, data):
    # This function will arrange our attributes how we like them.
    def build_info(path, xml):
        info = dict(xml.attrib.items())
        if 'file_name' in info:
            info['name'] = info.pop('file_name')
        else:
            info['is_dir'] = True
        info['path'] = abspath(pathjoin(path, info['name']))
        info['inode'] = info.pop('id')
        return info

    tree = {}
    # This function will build the tree recursively.
    def build_tree(path, xml):
        if hasattr(xml, 'folders'):
            for f in xml.folders[0].folder:
                info = build_info(path, f)
                tree[info['path']] = info
                build_tree(info['path'], f)
        if hasattr(xml, 'files'):
            for f in xml.files[0].file:
                info = build_info(path, f)
                tree[info['path']] = info

    # Base64 decode, then unzip and parse the XML into our tree.
    s = StringIO(base64.b64decode(data['tree']))
    with zipfile.ZipFile(s) as z:
        d = z.read(z.infolist()[0])
    xml = boxdotnet.XMLNode.parseXML(d)
    build_tree(path, xml)
    return tree


def get_xmlnode_attrs(xmlnode):
    for attr in dir(xmlnode):
        if attr.startswith('__') or attr in XMLNODE_IGNORE:
            continue
        yield attr


def xml_to_dict(xmlnode):
    "Convert XML nodes to a dictionary."
    d = {}
    for name in get_xmlnode_attrs(xmlnode):
        attr = getattr(xmlnode, name)
        if attr[0].elementText:
            value = attr[0].elementText
        else:
            value = xml_to_dict(attr[0])
        if value:
            d[name] = value
    return d


class SpooledWriter(BaseSpooledWriter):
    def close(self):
        # Need to flush temporary file (but not StringIO).
        if hasattr(self.temp, 'flush'):
            self.temp.flush()
        self.bytes = self.temp.tell()
        self.temp.seek(0)
        self.client.upload(self.path, self)
        self.temp.close()


class SimpleClient(object):
    """Makes the boxdotnet API client usable. Caches information to reduce
    round-trips"""

    # *~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~
    # API Client Wrapper.
    # *~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~

    class Authenticate(object):
        "Handles authentication for API calls."
        def __init__(self, client, callable):
            self.client = client
            self.callable = callable

        def __call__(self, *args, **kwargs):
            "Proxies call to original callable, with authentication information."
            if self.client.api_key:
                kwargs.setdefault('api_key', self.client.api_key)
            if self.client.auth_token:
                kwargs.setdefault('auth_token', self.client.auth_token)
            # Call the original callable, and convert XML node to a dict.
            return xml_to_dict(self.callable(self.client, *args, **kwargs))

    def __init__(self, api_key, auth_token=None):
        self.cache = PathMap()
        # Used to remember if we have the children for a directory or not.
        self.dcache = {}
        self.api_key = api_key
        self.auth_token = auth_token
        self.client = boxdotnet.BoxDotNet()

    def __getattr__(self, name):
        "Wraps callables in an Authenticate instance. Passes other attributes along."
        attr = self.client.__getattr__(name)
        if callable(attr):
            attr = SimpleClient.Authenticate(self, attr)
        return attr

    def get_ticket(self):
        """Break the login operation into two steps for non-interactive use.
        This is the first step, which creates a ticket."""
        import pdb; pdb.set_trace()
        t = self.client.get_ticket(api_key=self.api_key)
        return self.to_dict(t).get('ticket')

    def get_auth_token(self, ticket):
        """Break the login operation into two steps for non-interactive use.
        This is the second step, which retrieves the auth token."""
        a = self.client.get_auth_token(api_key=self.api_key, ticket=ticket)
        return self.to_dict(a).get('auth_token')

    def create_folder(self, path):
        create_folder = self.__getattr__('create_folder')
        dirname, basename = pathsplit(path)
        info = self.info(dirname)
        resp = create_folder(parent_id=info['inode'], name=basename, share=0)
        # Insert the new folder into our cache.
        info = {
            'path': path,
            'name': basename,
            'is_dir': True,
            'inode': resp['folder']['folder_id']
        }
        self.cache[path] = info

    def copy(self, src, dst):
        # A better copy, that keeps cache in sync.
        copy = self.__getattr__('move')
        src = self.info(src)
        dst = self.info(dst)
        if info['is_dir']:
            raise Exception('Cannot copy a directory (WTF!?).')
        copy(target='file', target_id=src['inode'], destination_id=dst['inode'])
        # API does not return the new id, so we can't update our cache.
        # We need to boot the destination from the cache to force a reload.
        self.dcache.pop(dst['path'], None)

    def move(self, src, dst):
        # A better copy, that keeps cache in sync.
        move = self.__getattr__('move')
        src = self.info(src)
        dst = self.info(dst)
        if info['is_dir']:
            raise Exception('Cannot copy a directory (WTF!?).')
        move(target='file', target_id=src['inode'], destination_id=dst['inode'])
        # API does not return the new id, so we can't update our cache.
        # We need to eject the destination from the cache to force a reload.
        self.dcache.pop(dst['path'], None)
        # The source, which no longer exists can be ejected.
        self.cache.pop(src['path'], None)
        self.dcache.pop(src['path'], None)

    def rename(self, src, dst):
        rename = self.__getattr__('rename')
        src = self.info(src)
        type = 'folder' if src['is_dir'] else 'file'
        move(target=type, target_id=src['inode'], new_name=dst)
        # Eject the parent directory from the cache.
        self.dcache.pop(dirname(src['path']), None)
        # Eject the src (renamed item) from the cache.
        self.cache.pop(src['path'], None)

    def upload(self, path, f):
        import pdb; pdb.set_trace()
        if isinstance(f, basestring):
            # upload given string as file's contents.
            f = StringIO(f)
        l = None
        try:
            l = len(f)
        except:
            try:
                l = os.fstat(f.fileno()).st_size
            except:
                try:
                    f.seek(0, 2)
                    l = f.tell()
                    f.seek(0)
                except:
                    raise Exception('Could not determine length of file!')
        dirname, basename = pathsplit(path)
        try:
            info = self.info(path)
        except:
            try:
                info = self.info(dirname)
            except:
                raise Exception('Cannot upload to non-existent directory!')
        url = '%s/%s/%s' % (ULURL, self.auth_token, info['inode'])
        host = urlparse.urlparse(url).hostname
        conn = httplib.HTTPConnection(host, 443)
        boundary = mimetools.choose_boundary()
        fields = {
            'boundary': boundary,
            'mime': mimetypes.guess_type(basename)[0] or 'application/octet-stream',
            'name': basename,
        }
        head = MULTIPART_HEAD % fields
        tail = MULTIPART_TAIL % fields
        l += len(head) + len(tail)
        headers = {
            'Content-Length': l,
            'Content-Type': 'multipart/form-data; boundary=%s' % boundary,
        }
        conn.request('POST', url, '', headers)
        # now stream the file to box.net.
        conn.send(head)
        while True:
            data = f.read(4096)
            if not data:
                break
            conn.send(data)
        conn.send(tail)
        r = conn.getresponse()
        if r.status != 200:
            raise Exception('Error uploading data!')

    def download(self, path):
        # Must open a URL according to:
        # http://developers.box.net/w/page/12923951/ApiFunction_Upload%20and%20Download
        info = self.info(path)
        url = '%s/%s/%s' % (DLURL, self.auth_token, info['inode'])
        return urllib.urlopen(url)

    def delete(self, path, file_only=False, dir_only=False):
        delete = self.__getattr__('delete')
        info = self.info(path)
        type = 'folder' if info['is_dir'] else 'file'
        if file_only and info['is_dir']:
            raise ResourceInvalidError(path)
        if dir_only and not info['is_dir']:
            raise ResourceInvalidError(path)
        delete(target=type, target_id=info['inode'])
        # Eject the deleted item from the cache.
        self.dcache.pop(path, None)
        self.cache.pop(path, None)

    # *~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~
    # Caching functionality.
    # *~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~*~

    def get_tree(self, path, folder_id, simple=True, onelevel=False, nofiles=False):
        "Performs an API call to box.net."
        params = []
        if simple:
            params.append('simple')
        if onelevel:
            params.append('onelevel')
        tree = self.get_account_tree(folder_id=folder_id, params=params)
        return xml_to_tree(path, tree)

    def get_ancestor(self, path):
        p, info = path, None
        while True:
            info = self.cache.get(p)
            if info or p in ('', '/'):
                break
            if not info:
                p, b = pathsplit(p)
        return info

    def get_subtree(self, path):
        "Gets the ancestor subtree closest to the given path."
        # We did not have the children in cache. We will now retrieve them. There
        # are three ways this will happen.
        # 1. If the parent is in the cache, we can use the inode (id) to fetch it's
        #    children, which will include the requested path.
        # 2. If an ancestor (besides the parent) is in the cache, we will fetch the
        #    subtree of that ancestor.
        # 3. If nothing is in the cache, we will grab the entire tree (from the root).
        info = self.get_ancestor(path)
        kwargs = {}
        if info:
            # We have one of the info's ancestors, we can limit our scope.
            inode = info['inode']
            parent = info['path']
        else:
            inode = 0
            parent = '/'
        # If one level will satisfy our needs, then do it.
        if parent in (dirname(path), path):
            kwargs['onelevel'] = True
        tree = self.get_tree(parent, inode, **kwargs)
        # Add all the infos to the cache.
        for k, v in tree.items():
            self.cache[k] = v
        # Put a dictionary in place as a placeholder for root.
        if parent == '/':
            self.cache[parent] = dict(is_dir=True, path='/', name='', size=0, inode=0)
        # Now add the children for each fetched info to the cache.
        if kwargs.get('onelevel'):
            # We use setdefault below because in the case of '/', there is no other
            # info. But for other paths, there may be.
            self.dcache[parent] = True
        else:
            # We have all the children for each of the keys we just added, so let's
            # add those now.
            for k in tree.keys():
                self.dcache[k] = True
        return tree

    def info(self, path):
        """Concerned with getting the information for a particular item. It will build up
        our cache in the process."""
        if not path in self.cache:
            self.get_subtree(path)
        info = self.cache.get(path)
        if not info:
            raise ResourceNotFoundError(path)
        # Copy the info so caller can't destroy anything.
        return dict(info.items())

    def list(self, path):
        """Concerned with listing directories. It will build up our cache in the process."""
        if not path in self.cache or not self.dcache.get(path):
            self.get_subtree(path)
        return self.cache.names(path)


def create_token(api_key):
    """Handles the oAuth workflow to enable access to a user's account.

    This only needs to be done initially, the token this function returns
    should then be stored and used in the future with create_client()."""
    c = SimpleClient(api_key)
    t = c.get_ticket()
    print "Please visit the following URL and authorize this application.\n"
    print AUTHURL % t
    print "\nWhen you are done, please press <enter>."
    raw_input()
    a = c.get_auth_token(ticket=t)
    print 'Your access token will be printed below, store it for later use.\n'
    print 'Access token:', a
    print "\nWhen you are done, please press <enter>."
    raw_input()
    return a


def create_client(api_key, auth_token):
    return SimpleClient(api_key, auth_token)


class BoxDotNetFS(FS):
    """A FileSystem that stores data in box.net, it uses the python API client.

    https://github.com/box/box-python-sdk"""

    _meta = { 'thread_safe' : True,
              'virtual' : False,
              'read_only' : False,
              'unicode_paths' : True,
              'case_insensitive_paths' : False,
              'network' : False,
              'atomic.setcontents' : False
             }

    def __init__(self, client, thread_synchronize=True):
        """Create an fs that interacts with BoxDotNet.

        :param client: the box.net API client (from the SDK).
        :param thread_synchronize: set to True (default) to enable thread-safety
        """
        super(BoxDotNetFS, self).__init__(thread_synchronize=thread_synchronize)
        # Used when we are writing information.
        self.client = client

    def __str__(self):
        return "<BoxDotNetFS: >"

    def __unicode__(self):
        return u"<BoxDotNetFS: >"

    def getmeta(self, meta_name, default=NoDefaultMeta):
        if meta_name == 'read_only':
            return self.read_only
        return super(ZipFS, self).getmeta(meta_name, default)

    @synchronize
    def open(self, path, mode="rb", **kwargs):
        path = normpath(path)
        if 'r' in mode:
            return self.client.download(path)
        else:
            return SpooledWriter(self.client, path)

    @synchronize
    def getcontents(self, path, mode="rb"):
        path = abspath(normpath(path))
        return self.open(self, path, mode).read()

    def setcontents(self, path, data, *args, **kwargs):
        path = abspath(normpath(path))
        self.client.upload(path, data, overwrite=True)

    def desc(self, path):
        return "%s in box.net" % path

    def getsyspath(self, path, allow_none=False):
        return abspath(normpath(path))

    def isdir(self, path):
        try:
            info = self.getinfo(path)
            return info.get('isdir', False)
        except ResourceNotFoundError:
            return False

    def isfile(self, path):
        try:
            info = self.getinfo(path)
            return not info.get('isdir', False)
        except ResourceNotFoundError:
            return False

    def exists(self, path):
        try:
            self.getinfo(path)
            return True
        except ResourceNotFoundError:
            return False

    def listdir(self, path="/", wildcard=None, full=False, absolute=False, dirs_only=False, files_only=False):
        path = abspath(normpath(path))
        list = self.client.list(path)
        return self._listdir_helper(path, list, wildcard, full, absolute, dirs_only, files_only)

    @synchronize
    def getinfo(self, path):
        path = abspath(normpath(path))
        try:
            info = self.client.info(path)
        except Exception, e:
            raise ResourceNotFoundError(path)
        info['isdir'] = info.pop('is_dir', False)
        info['isfile'] = not info['isdir']
        info.setdefault('size', 0)
        if path == '/':
            info['mime'] = 'virtual/boxnet'
        return info

    def copy(self, src, dst, *args, **kwargs):
        src = abspath(normpath(src))
        dst = abspath(normpath(dst))
        self.client.copy(src, dst)

    def copydir(self, src, dst, *args, **kwargs):
        src = abspath(normpath(src))
        dst = abspath(normpath(dst))
        self.client.copy(src, dst)

    def move(self, src, dst, *args, **kwargs):
        src = abspath(normpath(src))
        dst = abspath(normpath(dst))
        self.client.move(src, dst)

    def movedir(self, src, dst, *args, **kwargs):
        src = abspath(normpath(src))
        dst = abspath(normpath(dst))
        self.client.move(src, dst)

    def rename(self, src, dst, *args, **kwargs):
        src = abspath(normpath(src))
        dst = abspath(normpath(dst))
        self.client.rename(src, dst)

    def makedir(self, path, recursive=False, allow_recreate=False):
        path = abspath(normpath(path))
        try:
            self.client.create_folder(path)
        except boxdotnet.BoxDotNetError, e:
            if '[s_folder_exists]' in str(e):
                if not allow_recreate:
                    raise DestinationExistsError(path)
            else:
                raise

    def createfile(self, path, wipe=False):
        path = abspath(normpath(path))
        if not wipe and self.exists(path):
            return
        self.client.upload(path, '')

    def remove(self, path):
        path = abspath(normpath(path))
        self.client.delete(path, file_only=True)

    def removedir(self, path, *args, **kwargs):
        path = abspath(normpath(path))
        self.client.delete(path, dir_only=True)


def main():
    parser = optparse.OptionParser(prog="dropboxfs", description="CLI harness for DropboxFS.")
    parser.add_option("-k", "--api-key", help="Your box.net API key.")
    parser.add_option("-a", "--token", help="Your access token key (if you previously obtained one.")

    (options, args) = parser.parse_args()

    # Can't operate without these parameters.
    if not options.api_key:
        parser.error('You must obtain an api key from box.net.\nhttps://www.box.com/developers/services/edit/')

    # Instantiate a client one way or another.
    if not options.token:
        t = create_token(options.api_key)
        c = create_client(options.api_key, t)
    else:
        c = create_client(options.api_key, options.token)

    fs = BoxDotNetFS(c)
    print fs.listdir('/')
    print fs.listdir('/Test Folder')


if __name__ == '__main__':
    main()

